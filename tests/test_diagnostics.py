from vsphere_agent.diagnostics import RateLimitedDiagnostics


def test_first_periodic_summary_and_recovery_are_rate_limited_and_redacted():
    clock = [0]
    messages = []
    diagnostics = RateLimitedDiagnostics(messages.append, clock=lambda: clock[0], secrets=["test-secret"], interval=300)
    diagnostics.fault("es", "密码 test-secret", 1)
    diagnostics.fault("es", "密码 test-secret", 2)
    assert len(messages) == 1
    clock[0] = 300
    diagnostics.tick()
    assert len(messages) == 2 and "3" in messages[1]
    clock[0] = 301
    diagnostics.recover("es")
    assert len(messages) == 3
    assert all("test-secret" not in message for message in messages)


def test_fault_categories_are_bounded():
    messages = []
    diagnostics = RateLimitedDiagnostics(messages.append, clock=lambda: 0)
    diagnostics.fault("unknown-with-arbitrary-content", "failure")
    assert "unknown-with-arbitrary-content" not in messages[0]


def test_buffer_drop_episode_ends_after_a_quiet_interval():
    clock, messages = [0], []
    diagnostics = RateLimitedDiagnostics(messages.append, clock=lambda: clock[0], interval=300)
    diagnostics.fault("buffer", "超限")
    clock[0] = 250
    diagnostics.fault("buffer", "超限")
    clock[0] = 300
    diagnostics.tick()
    assert "持续" in messages[-1]
    clock[0] = 550
    diagnostics.tick()
    assert "未再发生丢弃" in messages[-1] and "2" in messages[-1]
    diagnostics.fault("buffer", "再次超限")
    assert "故障：" in messages[-1]
