from types import SimpleNamespace
from unittest.mock import patch
from application.ticket_system_handoff import buyer_labels


def test_profile_names_accept_scoped_platform_facts_and_business_order_objects():
    identity=dict(tenant_id='107',shop_id='s',buyer_id='b')
    with patch('application.record_view.buyer_names',return_value={('107','s','b'):'买家昵称'}) as lookup:
        names,pending=buyer_labels([identity],[SimpleNamespace(**identity)])
    assert names[('107','s','b')]=='买家昵称' and not pending
    lookup.assert_called_once_with([identity],wait=True)
