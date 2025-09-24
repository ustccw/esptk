
def setup(dut):
    pass

def loop(dut):
    dut.AT1.expect_ok('AT+GMR\r\n')
    dut.AT2.expect_ok('AT+GMR\r\n')
    dut.AT3.expect_ok('AT+GMR\r\n')
    pass

def teardown(dut):
    pass
