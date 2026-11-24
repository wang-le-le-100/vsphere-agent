from types import SimpleNamespace

from pyVmomi import vim

from vsphere_agent.config import VCenterConfig
from vsphere_agent.vcenter import VCenterAdapter


def test_entities_view_is_closed_and_per_entity_alarm_query_is_used():
    closed = []
    item = vim.VirtualMachine("vm-1", None)
    view = SimpleNamespace(view=[item], DestroyView=lambda: closed.append(True))
    manager = SimpleNamespace(CreateContainerView=lambda root, types, recursive: view)
    adapter = VCenterAdapter(VCenterConfig("vc.invalid", "user", "secret"))
    adapter.content = SimpleNamespace(rootFolder=object(), viewManager=manager, alarmManager=SimpleNamespace(GetAlarmState=lambda entity: [entity]))
    assert adapter.enumerate_entities() == [item]
    assert closed == [True]
    assert adapter.alarms_for(item) == [item]
