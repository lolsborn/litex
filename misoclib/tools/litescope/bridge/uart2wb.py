from misoclib.tools.litescope.common import *
from migen.bus import wishbone
from migen.genlib.misc import chooser
from migen.genlib.cdc import MultiReg
from migen.bank.description import *
from migen.bank.eventmanager import *
from migen.genlib.record import Record
from migen.flow.actor import Sink, Source

class UARTRX(Module):
	def __init__(self, pads, tuning_word):
		self.source = Source([("d", 8)])

		###

		uart_clk_rxen = Signal()
		phase_accumulator_rx = Signal(32)

		rx = Signal()
		self.specials += MultiReg(pads.rx, rx)
		rx_r = Signal()
		rx_reg = Signal(8)
		rx_bitcount = Signal(4)
		rx_busy = Signal()
		rx_done = self.source.stb
		rx_data = self.source.d
		self.sync += [
			rx_done.eq(0),
			rx_r.eq(rx),
			If(~rx_busy,
				If(~rx & rx_r, # look for start bit
					rx_busy.eq(1),
					rx_bitcount.eq(0),
				)
			).Else(
				If(uart_clk_rxen,
					rx_bitcount.eq(rx_bitcount + 1),
					If(rx_bitcount == 0,
						If(rx, # verify start bit
							rx_busy.eq(0)
						)
					).Elif(rx_bitcount == 9,
						rx_busy.eq(0),
						If(rx, # verify stop bit
							rx_data.eq(rx_reg),
							rx_done.eq(1)
						)
					).Else(
						rx_reg.eq(Cat(rx_reg[1:], rx))
					)
				)
			)
		]
		self.sync += \
				If(rx_busy,
					Cat(phase_accumulator_rx, uart_clk_rxen).eq(phase_accumulator_rx + tuning_word)
				).Else(
					Cat(phase_accumulator_rx, uart_clk_rxen).eq(2**31)
				)

class UARTTX(Module):
	def __init__(self, pads, tuning_word):
		self.sink = Sink([("d", 8)])

		###

		uart_clk_txen = Signal()
		phase_accumulator_tx = Signal(32)

		pads.tx.reset = 1

		tx_reg = Signal(8)
		tx_bitcount = Signal(4)
		tx_busy = Signal()
		self.sync += [
			self.sink.ack.eq(0),
			If(self.sink.stb & ~tx_busy & ~self.sink.ack,
				tx_reg.eq(self.sink.d),
				tx_bitcount.eq(0),
				tx_busy.eq(1),
				pads.tx.eq(0)
			).Elif(uart_clk_txen & tx_busy,
				tx_bitcount.eq(tx_bitcount + 1),
				If(tx_bitcount == 8,
					pads.tx.eq(1)
				).Elif(tx_bitcount == 9,
					pads.tx.eq(1),
					tx_busy.eq(0),
					self.sink.ack.eq(1),
				).Else(
					pads.tx.eq(tx_reg[0]),
					tx_reg.eq(Cat(tx_reg[1:], 0))
				)
			)
		]
		self.sync += [
				If(tx_busy,
					Cat(phase_accumulator_tx, uart_clk_txen).eq(phase_accumulator_tx + tuning_word)
				).Else(
					Cat(phase_accumulator_tx, uart_clk_txen).eq(0)
				)
		]

class UART(Module, AutoCSR):
	def __init__(self, pads, clk_freq, baud=115200):
		self._tuning_word = CSRStorage(32, reset=int((baud/clk_freq)*2**32))
		tuning_word = self._tuning_word.storage

		###

		self.rx = UARTRX(pads, tuning_word)
		self.tx = UARTTX(pads, tuning_word)
		self.submodules += self.rx, self.tx

class UARTPads:
	def __init__(self):
		self.rx = Signal()
		self.tx = Signal()

class UARTMux(Module):
	def __init__(self, pads):
		self.sel = Signal(max=2)
		self.shared_pads = UARTPads()
		self.bridge_pads = UARTPads()

	###
		# Route rx pad:
		# when sel==0, route it to shared rx and bridge rx
		# when sel==1, route it only to bridge rx
		self.comb += \
			If(self.sel==0,
				self.shared_pads.rx.eq(pads.rx),
				self.bridge_pads.rx.eq(pads.rx)
			).Else(
				self.bridge_pads.rx.eq(pads.rx)
			)

		# Route tx:
		# when sel==0, route shared tx to pads tx
		# when sel==1, route bridge tx to pads tx
		self.comb += \
			If(self.sel==0,
				pads.tx.eq(self.shared_pads.tx)
			).Else(
				pads.tx.eq(self.bridge_pads.tx)
			)

class LiteScopeUART2WB(Module, AutoCSR):
	cmds = {
		"write"	: 0x01,
		"read"	: 0x02
	}
	def __init__(self, pads, clk_freq, baud=115200, share_uart=False):
		self.wishbone = wishbone.Interface()
		if share_uart:
			self._sel = CSRStorage()
		###
		if share_uart:
			uart_mux = UARTMux(pads)
			uart = UART(uart_mux.bridge_pads, clk_freq, baud)
			self.submodules += uart_mux, uart
			self.shared_pads = uart_mux.shared_pads
			self.comb += uart_mux.sel.eq(self._sel.storage)
		else:
			uart = UART(pads, clk_freq, baud)
			self.submodules += uart

		byte_counter = Counter(bits_sign=3)
		word_counter = Counter(bits_sign=8)
		self.submodules += byte_counter, word_counter

		cmd = Signal(8)
		cmd_ce = Signal()

		length = Signal(8)
		length_ce = Signal()

		address = Signal(32)
		address_ce = Signal()

		data = Signal(32)
		rx_data_ce = Signal()
		tx_data_ce = Signal()

		self.sync += [
			If(cmd_ce, cmd.eq(uart.rx.source.d)),
			If(length_ce, length.eq(uart.rx.source.d)),
			If(address_ce, address.eq(Cat(uart.rx.source.d, address[0:24]))),
			If(rx_data_ce,
				data.eq(Cat(uart.rx.source.d, data[0:24]))
			).Elif(tx_data_ce,
				data.eq(self.wishbone.dat_r)
			)
		]

		###
		fsm = InsertReset(FSM(reset_state="IDLE"))
		timeout = Timeout(clk_freq//10)
		self.submodules += fsm, timeout
		self.comb += [
			timeout.ce.eq(1),
			fsm.reset.eq(timeout.reached)
		]
		fsm.act("IDLE",
			timeout.reset.eq(1),
			If(uart.rx.source.stb,
				cmd_ce.eq(1),
				If(	(uart.rx.source.d == self.cmds["write"]) |
					(uart.rx.source.d == self.cmds["read"]),
					NextState("RECEIVE_LENGTH")
				),
				byte_counter.reset.eq(1),
				word_counter.reset.eq(1)
			)
		)
		fsm.act("RECEIVE_LENGTH",
			If(uart.rx.source.stb,
				length_ce.eq(1),
				NextState("RECEIVE_ADDRESS")
			)
		)
		fsm.act("RECEIVE_ADDRESS",
			If(uart.rx.source.stb,
				address_ce.eq(1),
				byte_counter.ce.eq(1),
				If(byte_counter.value == 3,
					If(cmd == self.cmds["write"],
						NextState("RECEIVE_DATA")
					).Elif(cmd == self.cmds["read"],
						NextState("READ_DATA")
					),
					byte_counter.reset.eq(1),
				)
			)
		)
		fsm.act("RECEIVE_DATA",
			If(uart.rx.source.stb,
				rx_data_ce.eq(1),
				byte_counter.ce.eq(1),
				If(byte_counter.value == 3,
					NextState("WRITE_DATA"),
					byte_counter.reset.eq(1)
				)
			)
		)
		self.comb += [
			self.wishbone.adr.eq(address + word_counter.value),
			self.wishbone.dat_w.eq(data),
			self.wishbone.sel.eq(2**flen(self.wishbone.sel)-1)
		]
		fsm.act("WRITE_DATA",
			self.wishbone.stb.eq(1),
			self.wishbone.we.eq(1),
			self.wishbone.cyc.eq(1),
			If(self.wishbone.ack,
				word_counter.ce.eq(1),
				If(word_counter.value == (length-1),
					NextState("IDLE")
				).Else(
					NextState("RECEIVE_DATA")
				)
			)
		)
		fsm.act("READ_DATA",
			self.wishbone.stb.eq(1),
			self.wishbone.we.eq(0),
			self.wishbone.cyc.eq(1),
			If(self.wishbone.ack,
				tx_data_ce.eq(1),
				NextState("SEND_DATA")
			)
		)
		self.comb += \
			chooser(data, byte_counter.value, uart.tx.sink.d, n=4, reverse=True)
		fsm.act("SEND_DATA",
			uart.tx.sink.stb.eq(1),
			If(uart.tx.sink.ack,
				byte_counter.ce.eq(1),
				If(byte_counter.value == 3,
					word_counter.ce.eq(1),
					If(word_counter.value == (length-1),
						NextState("IDLE")
					).Else(
						NextState("READ_DATA"),
						byte_counter.reset.eq(1)
					)
				)
			)
		)