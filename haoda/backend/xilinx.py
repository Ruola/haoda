import collections
import contextlib
import glob
import logging
import os
import subprocess
import tarfile
import tempfile
import xml.etree.ElementTree as ET
import xml.sax.saxutils
import zipfile
from typing import (BinaryIO, Iterable, Iterator, Mapping, Optional, TextIO,
                    Tuple, Union)

from haoda import ir, util

_logger = logging.getLogger().getChild(__name__)


class Vivado(subprocess.Popen):
  """Call vivado with the given Tcl commands and arguments.

  This is a subclass of subprocess.Popen. A temporary directory will be created
  and used as the working directory.

  Args:
    commands: A string of Tcl commands.
    args: Iterable of strings as arguments to the Tcl commands.
  """

  def __init__(self, commands: str, *args: Iterable[str]):
    self.cwd = tempfile.TemporaryDirectory(prefix='vivado-')
    with open(os.path.join(self.cwd.name, 'commands.tcl'),
              mode='w+') as tcl_file:
      tcl_file.write(commands)
    cmd_args = [
        'vivado', '-mode', 'batch', '-source', tcl_file.name, '-nojournal',
        '-tclargs', *args
    ]
    pipe_args = {'stdout': subprocess.PIPE, 'stderr': subprocess.PIPE}
    super().__init__(cmd_args, cwd=self.cwd.name, **pipe_args)  # type: ignore

  def __exit__(self, *args) -> None:
    super().__exit__(*args)
    self.cwd.cleanup()


class VivadoHls(subprocess.Popen):
  """Call vivado_hls with the given Tcl commands.

  This is a subclass of subprocess.Popen. A temporary directory will be created
  and used as the working directory.

  Args:
    commands: A string of Tcl commands.
  """

  def __init__(self, commands: str):
    self.cwd = tempfile.TemporaryDirectory(prefix='vivado-hls-')
    with open(os.path.join(self.cwd.name, 'commands.tcl'),
              mode='w+') as tcl_file:
      tcl_file.write(commands)
    cmd_args = ['vivado_hls', '-f', tcl_file.name]
    pipe_args = {'stdout': subprocess.PIPE, 'stderr': subprocess.PIPE}
    super().__init__(cmd_args, cwd=self.cwd.name, **pipe_args)  # type: ignore

  def __exit__(self, *args) -> None:
    super().__exit__(*args)
    self.cwd.cleanup()


PACKAGEXO_COMMANDS = r'''
set tmp_ip_dir "{tmpdir}/tmp_ip_dir"
set tmp_project "{tmpdir}/tmp_project"

create_project -force kernel_pack ${{tmp_project}}
add_files -norecurse [glob {hdl_dir}/*.v]
foreach tcl_file [glob -nocomplain {hdl_dir}/*.tcl] {{
  source ${{tcl_file}}
}}
set_property top {top_name} [current_fileset]
update_compile_order -fileset sources_1
update_compile_order -fileset sim_1
ipx::package_project -root_dir ${{tmp_ip_dir}} -vendor xilinx.com -library RTLKernel -taxonomy /KernelIP -import_files -set_current false
ipx::unload_core ${{tmp_ip_dir}}/component.xml
ipx::edit_ip_in_project -upgrade true -name tmp_edit_project -directory ${{tmp_ip_dir}} ${{tmp_ip_dir}}/component.xml
set_property core_revision 2 [ipx::current_core]
foreach up [ipx::get_user_parameters] {{
  ipx::remove_user_parameter [get_property NAME ${{up}}] [ipx::current_core]
}}
set_property sdx_kernel true [ipx::current_core]
set_property sdx_kernel_type rtl [ipx::current_core]
ipx::create_xgui_files [ipx::current_core]
{bus_ifaces}
set_property xpm_libraries {{XPM_CDC XPM_MEMORY XPM_FIFO}} [ipx::current_core]
set_property supported_families {{ }} [ipx::current_core]
set_property auto_family_support_level level_2 [ipx::current_core]
ipx::update_checksums [ipx::current_core]
ipx::save_core [ipx::current_core]
close_project -delete

package_xo -force -xo_path "{xo_file}" -kernel_name {top_name} -ip_directory ${{tmp_ip_dir}} -kernel_xml {kernel_xml}{cpp_kernels}
'''

BUS_IFACE = r'''
ipx::associate_bus_interfaces -busif {} -clock ap_clk [ipx::current_core]
'''


class PackageXo(Vivado):
  """Packages the given files into a Xilinx hardware object.

  This is a subclass of subprocess.Popen. A temporary directory will be created
  and used as the working directory.

  Args:
    xo_file: Name of the generated xo file.
    top_name: Top-level module name.
    kernel_xml: Name of a xml file containing description of the kernel.
    hdl_dir: Directory name containing all HDL files.
    m_axi_names: Variable names connected to the m_axi bus.
    iface_names: Other interface names, default to ('s_axi_control').
    cpp_kernels: File names of C++ kernels.
  """

  def __init__(self,
               xo_file: str,
               top_name: str,
               kernel_xml: str,
               hdl_dir: str,
               m_axi_names: Iterable[str] = (),
               iface_names: Iterable[str] = ('s_axi_control',),
               cpp_kernels=()):
    self.tmpdir = tempfile.TemporaryDirectory(prefix='package-xo-')
    if _logger.isEnabledFor(logging.INFO):
      for _, _, files in os.walk(hdl_dir):
        for filename in files:
          _logger.info('packing: %s', filename)
    iface_names = list(iface_names)
    iface_names.extend(map('m_axi_{}'.format, m_axi_names))
    kwargs = {
        'top_name': top_name,
        'kernel_xml': kernel_xml,
        'hdl_dir': hdl_dir,
        'xo_file': xo_file,
        'bus_ifaces': ''.join(map(BUS_IFACE.format, iface_names)),
        'tmpdir': self.tmpdir.name,
        'cpp_kernels': ''.join(map(' -kernel_files {}'.format, cpp_kernels))
    }
    super().__init__(PACKAGEXO_COMMANDS.format(**kwargs))

  def __exit__(self, *args) -> None:
    super().__exit__(*args)
    self.tmpdir.cleanup()


HLS_COMMANDS = r'''
cd "{project_dir}"
open_project "{project_name}"
set_top {top_name}
{add_kernels}
open_solution "{solution_name}"
set_part {{{part_num}}}
create_clock -period {clock_period} -name default
config_compile -name_max_length 253
config_interface -m_axi_addr64
config_rtl -disable_start_propagation -reset_level {reset_level}
csynth_design
exit
'''


class RunHls(VivadoHls):
  """Runs Vivado HLS for the given kernels and generate HDL files

  This is a subclass of subprocess.Popen. A temporary directory will be created
  and used as the working directory.

  Args:
    tarfileobj: File object that will contain the reports and HDL files.
    kernel_files: File names or tuple of file names and cflags of the kernels.
    top_name: Top-level module name.
    clock_period: Target clock period.
    part_num: Target part number.
  """

  def __init__(self,
               tarfileobj: BinaryIO,
               kernel_files: Iterable[Union[str, Tuple[str, str]]],
               top_name: str,
               clock_period: str,
               part_num: str,
               reset_low: bool = True):
    self.project_dir = tempfile.TemporaryDirectory(prefix='run-hls-')
    self.project_name = 'project'
    self.solution_name = top_name
    self.tarfileobj = tarfileobj
    kernels = []
    for kernel_file in kernel_files:
      if isinstance(kernel_file, str):
        kernels.append(
            'add_files "{}" -cflags "-std=c++11"'.format(kernel_file))
      else:
        kernels.append(
            'add_files "{}" -cflags "-std=c++11 {}"'.format(*kernel_file))
    kwargs = {
        'project_dir': self.project_dir.name,
        'project_name': self.project_name,
        'solution_name': self.solution_name,
        'top_name': top_name,
        'add_kernels': '\n'.join(kernels),
        'part_num': part_num,
        'clock_period': clock_period,
        'reset_level': 'low' if reset_low else 'high',
    }
    super().__init__(HLS_COMMANDS.format(**kwargs))

  def __exit__(self, *args):
    self.wait()
    if self.returncode == 0:
      with tarfile.open(mode='w', fileobj=self.tarfileobj) as tar:
        solution_dir = os.path.join(self.project_dir.name, self.project_name,
                                    self.solution_name)
        try:
          tar.add(os.path.join(solution_dir, 'syn/report'), arcname='report')
          tar.add(os.path.join(solution_dir, 'syn/verilog'), arcname='hdl')
          tar.add(os.path.join(solution_dir, self.cwd.name, 'vivado_hls.log'),
                  arcname='log/' + self.solution_name + '.log')
          for pattern in ('*.sched.adb.xml', '*.verbose.sched.rpt',
                          '*.verbose.sched.rpt.xml'):
            for f in glob.glob(
                os.path.join(solution_dir, '.autopilot', 'db', pattern)):
              tar.add(f, arcname='report/' + os.path.basename(f))
        except FileNotFoundError as e:
          self.returncode = 1
          _logger.error('%s', e)
    super().__exit__(*args)
    self.project_dir.cleanup()


XILINX_XML_NS = {'xd': 'http://www.xilinx.com/xd'}


def get_device_info(platform_path: str):
  """Extract device part number and target frequency from SDAccel platform.

  Currently only support 5.x platforms.

  Args:
    platform_path: Path to the platform directory, e.g.,
        '/opt/xilinx/platforms/xilinx_u200_qdma_201830_2'.

  Raises:
    ValueError: If cannot parse the platform properly.
  """
  device_name = os.path.basename(platform_path)
  with zipfile.ZipFile(os.path.join(platform_path, 'hw',
                                    device_name + '.dsa')) as platform:
    with platform.open(device_name + '.hpfm') as metadata:
      platform_info = ET.parse(metadata).find('./xd:component/xd:platformInfo',
                                              XILINX_XML_NS)
      if platform_info is None:
        raise ValueError('cannot parse platform')
      clock_period = platform_info.find(
          "./xd:systemClocks/xd:clock/[@xd:id='0']", XILINX_XML_NS)
      if clock_period is None:
        raise ValueError('cannot find clock period in platform')
      part_num = platform_info.find('xd:deviceInfo', XILINX_XML_NS)
      if part_num is None:
        raise ValueError('cannot find part number in platform')
      return {
          'clock_period':
              clock_period.attrib['{{{xd}}}period'.format(**XILINX_XML_NS)],
          'part_num':
              part_num.attrib['{{{xd}}}name'.format(**XILINX_XML_NS)]
      }


KERNEL_XML_TEMPLATE = r'''
<?xml version="1.0" encoding="UTF-8"?>
<root versionMajor="1" versionMinor="6">
  <kernel name="{top_name}" language="ip_c" vlnv="xilinx.com:RTLKernel:{top_name}:1.0" attributes="" preferredWorkGroupSizeMultiple="0" workGroupSize="1" interrupt="true">
    <ports>{ports}
    </ports>
    <args>{args}
    </args>
  </kernel>
</root>
'''

S_AXI_PORT = r'''
      <port name="s_axi_control" mode="slave" range="0x1000" dataWidth="32" portType="addressable" base="0x0"/>
'''

M_AXI_PORT_TEMPLATE = r'''
      <port name="m_axi_{name}" mode="master" range="0xFFFFFFFF" dataWidth="{width}" portType="addressable" base="0x0"/>
'''

AXIS_PORT_TEMPLATE = r'''
      <port name="{name}" mode="{mode}" dataWidth="{width}" portType="stream"/>
'''

ARG_TEMPLATE = r'''
      <arg name="{name}" addressQualifier="{addr_qualifier}" id="{arg_id}" port="{port_name}" size="{size:#x}" offset="{offset:#x}" hostOffset="0x0" hostSize="{host_size:#x}" type="{c_type}"/>
'''


def print_kernel_xml(top_name: str, axis_inputs: Iterable[Tuple[str, str,
                                                                ir.Type, str]],
                     axis_outputs: Iterable[Tuple[str, str, ir.Type,
                                                  str]], kernel_xml: TextIO):
  """Generate kernel.xml file.

  Args:
    top_name: Name of the top-level kernel function.
    axis_inputs: Sequence of (port_name, _, haoda_type, _) of input axis ports
    axis_outputs: Sequence of (port_name, _, haoda_type, _) of output axis ports
    kernel_xml: File object to write to.
  """
  ports = ''
  args = ''
  offset = 0
  arg_id = 0
  size = host_size = 8
  for mode, axis_ports in (('read_only', axis_inputs), ('write_only',
                                                        axis_outputs)):
    for port_name, _, haoda_type, _ in axis_ports:
      width = haoda_type.width_in_bits
      c_type = xml.sax.saxutils.escape('stream<ap_axiu<%d, 0, 0, 0>>&' % width)
      width += 8 + width // 8 * 2
      ports += AXIS_PORT_TEMPLATE.format(name=port_name, mode=mode,
                                         width=width).rstrip('\n')
      args += ARG_TEMPLATE.format(name=port_name,
                                  addr_qualifier=4,
                                  arg_id=arg_id,
                                  port_name=port_name,
                                  c_type=c_type,
                                  size=size,
                                  offset=offset,
                                  host_size=host_size).rstrip('\n')
    arg_id += 1
  kernel_xml.write(
      KERNEL_XML_TEMPLATE.format(top_name=top_name, ports=ports, args=args))


BRAM_FIFO_TEMPLATE = '''`default_nettype none

// first-word fall-through (FWFT) FIFO using block RAM
// based on HLS generated code
module {name} #(
  parameter MEM_STYLE  = "block",
  parameter DATA_WIDTH = {width},
  parameter ADDR_WIDTH = {addr_width},
  parameter DEPTH      = {depth}
) (
  input wire clk,
  input wire reset,

  // write
  output wire                  if_full_n,
  input  wire                  if_write_ce,
  input  wire                  if_write,
  input  wire [DATA_WIDTH-1:0] if_din,

  // read
  output wire                  if_empty_n,
  input  wire                  if_read_ce,
  input  wire                  if_read,
  output wire [DATA_WIDTH-1:0] if_dout
);

(* ram_style = MEM_STYLE *)
reg  [DATA_WIDTH-1:0] mem[0:DEPTH-1];
reg  [DATA_WIDTH-1:0] q_buf;
reg  [ADDR_WIDTH-1:0] waddr;
reg  [ADDR_WIDTH-1:0] raddr;
wire [ADDR_WIDTH-1:0] wnext;
wire [ADDR_WIDTH-1:0] rnext;
wire                  push;
wire                  pop;
reg  [ADDR_WIDTH-1:0] used;
reg                   full_n;
reg                   empty_n;
reg  [DATA_WIDTH-1:0] q_tmp;
reg                   show_ahead;
reg  [DATA_WIDTH-1:0] dout_buf;
reg                   dout_valid;

localparam DepthM1 = DEPTH[ADDR_WIDTH-1:0] - 1'd1;

assign if_full_n  = full_n;
assign if_empty_n = dout_valid;
assign if_dout    = dout_buf;
assign push       = full_n & if_write_ce & if_write;
assign pop        = empty_n & if_read_ce & (~dout_valid | if_read);
assign wnext      = !push              ? waddr              :
                    (waddr == DepthM1) ? {{ADDR_WIDTH{{1'b0}}}} : waddr + 1'd1;
assign rnext      = !pop               ? raddr              :
                    (raddr == DepthM1) ? {{ADDR_WIDTH{{1'b0}}}} : raddr + 1'd1;

// waddr
always @(posedge clk) begin
  if (reset)
    waddr <= {{ADDR_WIDTH{{1'b0}}}};
  else
    waddr <= wnext;
end

// raddr
always @(posedge clk) begin
  if (reset)
    raddr <= {{ADDR_WIDTH{{1'b0}}}};
  else
    raddr <= rnext;
end

// used
always @(posedge clk) begin
  if (reset)
    used <= {{ADDR_WIDTH{{1'b0}}}};
  else if (push && !pop)
    used <= used + 1'b1;
  else if (!push && pop)
    used <= used - 1'b1;
end

// full_n
always @(posedge clk) begin
  if (reset)
    full_n <= 1'b1;
  else if (push && !pop)
    full_n <= (used != DepthM1);
  else if (!push && pop)
    full_n <= 1'b1;
end

// empty_n
always @(posedge clk) begin
  if (reset)
    empty_n <= 1'b0;
  else if (push && !pop)
    empty_n <= 1'b1;
  else if (!push && pop)
    empty_n <= (used != {{{{(ADDR_WIDTH-1){{1'b0}}}},1'b1}});
end

// mem
always @(posedge clk) begin
  if (push)
    mem[waddr] <= if_din;
end

// q_buf
always @(posedge clk) begin
  q_buf <= mem[rnext];
end

// q_tmp
always @(posedge clk) begin
  if (reset)
    q_tmp <= {{DATA_WIDTH{{1'b0}}}};
  else if (push)
    q_tmp <= if_din;
end

// show_ahead
always @(posedge clk) begin
  if (reset)
    show_ahead <= 1'b0;
  else if (push && used == {{{{(ADDR_WIDTH-1){{1'b0}}}},pop}})
    show_ahead <= 1'b1;
  else
    show_ahead <= 1'b0;
end

// dout_buf
always @(posedge clk) begin
  if (reset)
    dout_buf <= {{DATA_WIDTH{{1'b0}}}};
  else if (pop)
    dout_buf <= show_ahead? q_tmp : q_buf;
end

// dout_valid
always @(posedge clk) begin
  if (reset)
    dout_valid <= 1'b0;
  else if (pop)
    dout_valid <= 1'b1;
  else if (if_read_ce & if_read)
    dout_valid <= 1'b0;
end

endmodule  // fifo_bram

`default_nettype wire
'''

SRL_FIFO_TEMPLATE = '''`default_nettype none

// first-word fall-through (FWFT) FIFO using shift register LUT
// based on HLS generated code
module {name} #(
  parameter MEM_STYLE  = "shiftreg",
  parameter DATA_WIDTH = {width},
  parameter ADDR_WIDTH = {addr_width},
  parameter DEPTH      = {depth}
) (
  input wire clk,
  input wire reset,

  // write
  output wire                  if_full_n,
  input  wire                  if_write_ce,
  input  wire                  if_write,
  input  wire [DATA_WIDTH-1:0] if_din,

  // read
  output wire                  if_empty_n,
  input  wire                  if_read_ce,
  input  wire                  if_read,
  output wire [DATA_WIDTH-1:0] if_dout
);

  wire [ADDR_WIDTH - 1:0] shift_reg_addr;
  wire [DATA_WIDTH - 1:0] shift_reg_data;
  wire [DATA_WIDTH - 1:0] shift_reg_q;
  wire                    shift_reg_ce;
  reg  [ADDR_WIDTH:0]     out_ptr;
  reg                     internal_empty_n;
  reg                     internal_full_n;

  reg [DATA_WIDTH-1:0] mem [0:DEPTH-1];

  assign if_empty_n = internal_empty_n;
  assign if_full_n = internal_full_n;
  assign shift_reg_data = if_din;
  assign if_dout = shift_reg_q;

  assign shift_reg_addr = out_ptr[ADDR_WIDTH] == 1'b0 ? out_ptr[ADDR_WIDTH-1:0] : {{ADDR_WIDTH{{1'b0}}}};
  assign shift_reg_ce = (if_write & if_write_ce) & internal_full_n;

  assign shift_reg_q = mem[shift_reg_addr];

  always @(posedge clk) begin
    if (reset) begin
      out_ptr <= ~{{ADDR_WIDTH+1{{1'b0}}}};
      internal_empty_n <= 1'b0;
      internal_full_n <= 1'b1;
    end else begin
      if (((if_read && if_read_ce) && internal_empty_n) &&
          (!(if_write && if_write_ce) || !internal_full_n)) begin
        out_ptr <= out_ptr - 1'b1;
        if (out_ptr == {{(ADDR_WIDTH+1){{1'b0}}}})
          internal_empty_n <= 1'b0;
        internal_full_n <= 1'b1;
      end
      else if (((if_read & if_read_ce) == 0 | internal_empty_n == 0) &&
        ((if_write & if_write_ce) == 1 & internal_full_n == 1))
      begin
        out_ptr <= out_ptr + 1'b1;
        internal_empty_n <= 1'b1;
        if (out_ptr == DEPTH - {{{{(ADDR_WIDTH-1){{1'b0}}}}, 2'd2}})
          internal_full_n <= 1'b0;
      end
    end
  end

  integer i;
  always @(posedge clk) begin
    if (shift_reg_ce) begin
      for (i = 0; i < DEPTH - 1; i = i + 1)
        mem[i + 1] <= mem[i];
      mem[0] <= shift_reg_data;
    end
  end

endmodule  // fifo_srl

`default_nettype wire
'''

AUTO_FIFO_TEMPLATE = '''`default_nettype none

// first-word fall-through (FWFT) FIFO
// if its capacity > 1024 bits, it uses block RAM, otherwise it will uses shift
// register LUT
module {name} #(
  parameter DATA_WIDTH = 32,
  parameter ADDR_WIDTH = 5,
  parameter DEPTH      = 32
) (
  input wire clk,
  input wire reset,

  // write
  output wire                  if_full_n,
  input  wire                  if_write_ce,
  input  wire                  if_write,
  input  wire [DATA_WIDTH-1:0] if_din,

  // read
  output wire                  if_empty_n,
  input  wire                  if_read_ce,
  input  wire                  if_read,
  output wire [DATA_WIDTH-1:0] if_dout
);

generate
  if (DATA_WIDTH * DEPTH > 1024) begin : bram
    fifo_bram #(
      .DATA_WIDTH(DATA_WIDTH),
      .ADDR_WIDTH(ADDR_WIDTH),
      .DEPTH     (DEPTH)
    ) unit (
      .clk  (clk),
      .reset(reset),

      .if_full_n  (if_full_n),
      .if_write_ce(if_write_ce),
      .if_write   (if_write),
      .if_din     (if_din),

      .if_empty_n(if_empty_n),
      .if_read_ce(if_read_ce),
      .if_read   (if_read),
      .if_dout   (if_dout)
    );
  end else begin : srl
    fifo_srl #(
      .DATA_WIDTH(DATA_WIDTH),
      .ADDR_WIDTH(ADDR_WIDTH),
      .DEPTH     (DEPTH)
    ) unit (
      .clk  (clk),
      .reset(reset),

      .if_full_n  (if_full_n),
      .if_write_ce(if_write_ce),
      .if_write   (if_write),
      .if_din     (if_din),

      .if_empty_n(if_empty_n),
      .if_read_ce(if_read_ce),
      .if_read   (if_read),
      .if_dout   (if_dout)
    );
  end
endgenerate

endmodule  // fifo

`default_nettype wire
'''


class VerilogPrinter(util.Printer):
  """A text-based Verilog printer."""

  def module(self, module_name: str, args: Iterable[str]) -> None:
    self.println('module %s (' % module_name)
    self.do_indent()
    self._out.write(' ' * self._indent * self._tab)
    self._out.write((',\n' + ' ' * self._indent * self._tab).join(args))
    self.un_indent()
    self.println('\n);')

  def endmodule(self, module_name: Optional[str] = None) -> None:
    if module_name is None:
      self.println('endmodule')
    else:
      self.println('endmodule // %s' % module_name)

  def begin(self) -> None:
    self.println('begin')
    self.do_indent()

  def end(self) -> None:
    self.un_indent()
    self.println('end')

  def parameter(self, key: str, value: str):
    self.println('parameter {} = {};'.format(key, value))

  @contextlib.contextmanager
  def initial(self) -> Iterator[None]:
    self.println('initial begin')
    self.do_indent()
    yield
    self.un_indent()
    self.println('end')

  @contextlib.contextmanager
  def always(self, condition: str) -> Iterator[None]:
    self.println('always @ (%s) begin' % condition)
    self.do_indent()
    yield
    self.un_indent()
    self.println('end')

  @contextlib.contextmanager
  def if_(self, condition: str) -> Iterator[None]:
    self.println('if (%s) begin' % condition)
    self.do_indent()
    yield
    self.end()

  def else_(self) -> None:
    self.un_indent()
    self.println('end else begin')
    self.do_indent()

  def module_instance(self, module_name: str, instance_name: str,
                      args: Union[Mapping[str, str], Iterable[str]]) -> None:
    self.println('{module_name} {instance_name}('.format(**locals()))
    self.do_indent()
    if isinstance(args, collections.Mapping):
      self._out.write(',\n'.join(' ' * self._indent * self._tab +
                                 '.{}({})'.format(*arg)
                                 for arg in args.items()))
    else:
      self._out.write(',\n'.join(
          ' ' * self._indent * self._tab + arg for arg in args))
    self.un_indent()
    self.println('\n);')

  def fifo_module(self,
                  width: int,
                  depth: int,
                  name: str = '',
                  threshold: int = 1024) -> None:
    """Generate FIFO with the given parameters.

    Generate an FIFO module. If its capacity is larger than threshold, BRAM FIFO
        will be used. Otherwise, SRL FIFO will be used.

    Args:
      width: FIFO width.
      depth: FIFO depth.
      name: Optionally give the fifo a name, default to
          'fifo_w{width}_d{depth}_A'.
      threshold: Optionally give a threshold to decide whether to use BRAM or
          SRL. Defaults to 1024 bits.

    Raises:
      ValueError: If depth or width is invalid.
    """
    if width * depth > threshold:
      self.bram_fifo_module(width, depth)
    else:
      self.srl_fifo_module(width, depth)

  def bram_fifo_module(self, width: int, depth: int, name: str = '') -> None:
    """Generate BRAM FIFO with the given parameters.

    Generate a BRAM FIFO module.

    Args:
      width: FIFO width.
      depth: FIFO depth.
      name: Optionally give the fifo a name, default to
          'fifo_w{width}_d{depth}_A'.

    Raises:
      ValueError: If depth or width is invalid.
    """
    if depth < 2:
      raise ValueError('Invalid BRAM FIFO depth: %d < 1' % depth)
    if not name:
      name = 'fifo_w{width}_d{depth}_A'.format(width=width, depth=depth)
    self._out.write(
        BRAM_FIFO_TEMPLATE.format(width=width,
                                  depth=depth,
                                  name=name,
                                  addr_width=(depth - 1).bit_length()))

  def srl_fifo_module(self, width: int, depth: int, name: str = '') -> None:
    """Generate SRL FIFO with the given parameters.

    Generate a SRL FIFO module.

    Args:
      width: FIFO width.
      depth: FIFO depth.
      name: Optionally give the fifo a name, default to
          'fifo_w{width}_d{depth}_A'.

    Raises:
      ValueError: If depth or width is invalid.
    """
    if depth < 2:
      raise ValueError('Invalid SRL FIFO depth: %d < 1' % depth)
    if not name:
      name = 'fifo_w{width}_d{depth}_A'.format(width=width, depth=depth)
    addr_width = (depth - 1).bit_length()
    self._out.write(
        SRL_FIFO_TEMPLATE.format(width=width,
                                 depth=depth,
                                 name=name,
                                 addr_width=addr_width,
                                 depth_width=addr_width + 1))
