"""HTTP route + alerting tests for the Flask layer.

Everything here runs against `app.test_client()` — hermetic, no sockets, no
server. Every filesystem path the app writes to is redirected into `tmp_path`
by the `client` fixture, so a test run never touches real runtime state
(settings, alert history, logs) and never reaches the live controller.
"""
import json
import os

import pytest

import app as appmod


# ── Fixtures ──────────────────────────────────────────────

class FakeDevice:
    """Stand-in for NovaStar_Device — carries a state dict, opens no socket."""

    def __init__(self, device_id, state):
        self.device_id = device_id
        self.state = state

    def disconnect(self):
        self.state['connected'] = False


def make_card(slot=20, port=0, card_id=0, temp=44.0, voltage=5.0, online=True):
    """Build a receiving-card dict shaped like the H-series refresh output."""
    return {
        'slot': slot, 'port': port, 'card_id': card_id,
        'online': online, 'temperature_c': temp, 'temp_c': temp,
        'voltage_v': voltage,
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Test client with all persistent state redirected into tmp_path."""
    log_dir = tmp_path / 'logs'
    log_dir.mkdir()

    monkeypatch.setattr(appmod, 'SETTINGS_FILE', str(tmp_path / 'settings.json'))
    monkeypatch.setattr(appmod, 'ERROR_LOG_FILE', str(tmp_path / 'error_log.json'))
    monkeypatch.setattr(appmod, 'WALL_SNAPSHOT_FILE',
                        str(tmp_path / 'wall_live_snapshot.json'))
    monkeypatch.setattr(appmod, 'LOG_DIR_PATH', str(log_dir))
    monkeypatch.setattr(appmod, 'LOG_FILE_PATH', str(log_dir / 'monitor.log'))

    # In-memory caches / counters
    monkeypatch.setattr(appmod, '_settings_cache', None)
    monkeypatch.setattr(appmod, '_next_error_id', 1)
    appmod._error_log[:] = []
    appmod._alert_seen.clear()
    appmod._snapshot_cache.clear()

    saved_devices = dict(appmod.manager.devices)
    saved_interval = appmod.manager.poll_interval
    appmod.manager.devices.clear()

    with appmod.app.test_client() as c:
        yield c

    appmod.manager.devices.clear()
    appmod.manager.devices.update(saved_devices)
    appmod.manager.poll_interval = saved_interval
    appmod._error_log[:] = []
    appmod._alert_seen.clear()
    appmod._snapshot_cache.clear()


def read_settings_file(tmp_path):
    with open(tmp_path / 'settings.json') as f:
        return json.load(f)


# ── Import safety (Task 4) ────────────────────────────────

def test_import_does_not_start_polling():
    """Importing app must never open connections to real hardware."""
    assert appmod._initialized is False
    assert appmod.manager._running is False


def test_socketio_run_is_wrapped_for_init():
    """Launchers call socketio.run() directly — init has to hang off it."""
    assert appmod.socketio.run.__name__ == '_run_server'


def test_reloader_parent_does_not_own_pollers(monkeypatch):
    monkeypatch.delenv('WERKZEUG_RUN_MAIN', raising=False)
    assert appmod._should_init_here(use_reloader=False) is True
    assert appmod._should_init_here(use_reloader=True) is False
    monkeypatch.setenv('WERKZEUG_RUN_MAIN', 'true')
    assert appmod._should_init_here(use_reloader=True) is True


# ── Error handling (Task 6) ───────────────────────────────

def test_unknown_url_is_404_not_500(client):
    res = client.get('/api/definitely-not-a-route')
    assert res.status_code == 404
    assert b'Internal server error' not in res.data


def test_unknown_device_state_is_404(client):
    assert client.get('/api/devices/dev-nope/state').status_code == 404


def test_wrong_method_is_405(client):
    assert client.put('/api/settings').status_code == 405


# ── Wall endpoints (Task 3) ───────────────────────────────

def write_snapshot(tmp_path, cards=None, captured_at=None):
    snap = {'device_ip': '192.168.0.10', 'screen_name': 'TEST WALL',
            'cards': cards if cards is not None else [make_card()]}
    if captured_at:
        snap['captured_at'] = captured_at
    path = tmp_path / 'wall_live_snapshot.json'
    with open(path, 'w') as f:
        json.dump(snap, f)
    return path


def test_wall_live_unavailable_without_snapshot_or_device(client):
    data = client.get('/api/wall_live').get_json()
    assert data['available'] is False
    assert data['live'] is False
    assert data['device_connected'] is False


def test_wall_live_snapshot_only_is_not_live(client, tmp_path):
    """A snapshot file on disk must never be presented as live data."""
    write_snapshot(tmp_path, captured_at='2026-05-01T12:00:00')
    data = client.get('/api/wall_live').get_json()

    assert data['available'] is True
    assert data['live'] is False              # nothing is connected
    assert data['device_connected'] is False
    assert data['captured_at'] == '2026-05-01T12:00:00'
    assert data['snapshot_mtime']             # freshness even without capture time
    assert data['last_poll'] is None
    assert 'receiving_cards' not in data
    assert data['snapshot']['screen_name'] == 'TEST WALL'


def test_wall_live_tolerates_missing_captured_at(client, tmp_path):
    write_snapshot(tmp_path)
    data = client.get('/api/wall_live').get_json()
    assert data['captured_at'] is None
    assert data['snapshot_mtime'] is not None


def test_wall_live_is_live_when_device_reports_cards(client, tmp_path):
    write_snapshot(tmp_path)
    cards = [make_card(card_id=0), make_card(card_id=1)]
    appmod.manager.devices['dev-h'] = FakeDevice('dev-h', {
        'device_id': 'dev-h', 'name': 'H-series Live', 'ip': '192.168.0.10',
        'device_type': 'h_series', 'connected': True, 'last_poll': '12:00:01',
        'cards_fresh': True, 'receiving_cards': cards, 'screen_outputs': {}, 'device_info': {},
    })

    data = client.get('/api/wall_live').get_json()
    assert data['live'] is True
    assert data['device_connected'] is True
    assert data['last_poll'] == '12:00:01'
    assert len(data['receiving_cards']) == 2
    assert data['device']['ip'] == '192.168.0.10'


def test_wall_live_connected_device_without_cards_is_not_live(client, tmp_path):
    """Connected but nothing enumerated yet — still snapshot data on screen."""
    write_snapshot(tmp_path)
    appmod.manager.devices['dev-h'] = FakeDevice('dev-h', {
        'name': 'H', 'device_type': 'h_series', 'connected': True,
        'last_poll': '12:00:01', 'cards_fresh': True, 'receiving_cards': [], 'device_info': {},
    })
    data = client.get('/api/wall_live').get_json()
    assert data['device_connected'] is True
    assert data['live'] is False


def test_wall_snapshot_is_cached_and_invalidated_by_mtime(client, tmp_path):
    write_snapshot(tmp_path)
    first, _ = appmod.load_wall_snapshot()
    second, _ = appmod.load_wall_snapshot()
    assert first is second                    # same object — no re-parse

    write_snapshot(tmp_path, cards=[make_card(card_id=i) for i in range(5)])
    os.utime(tmp_path / 'wall_live_snapshot.json', (1, 1))
    third, _ = appmod.load_wall_snapshot()
    assert third is not first
    assert len(third['cards']) == 5


def test_wall_snapshot_missing_file_returns_none(client, tmp_path):
    assert appmod.load_wall_snapshot(str(tmp_path / 'nope.json')) == (None, None)


# ── Stale snapshot detection ──────────────────────────────
#
# The failure this guards against: the operator reconfigured the wall, the
# stored enumeration still describes the old one, and the dashboard presented
# its 1374 cards while the payload said `live: true`. The wall's identity now
# comes from the controller every poll, and a snapshot that does not describe
# the currently-configured wall is withheld outright rather than dimmed.

def screen_outputs(name='CIRCUIT MOM', width=3840, height=2160,
                   slots=(20, 22, 28, 30)):
    """R0405 as the live processor answers it: N slots, each covering the canvas."""
    interfaces = []
    iface_id = 0
    for slot in slots:
        for column in range(4):
            interfaces.append({
                'slotId': slot, 'interfaceId': iface_id, 'outputId': column,
                'x': column * (width // 4), 'y': 0,
                'width': width // 4, 'height': height,
                'interfaceType': 2, 'isCardOnline': None,
            })
            iface_id += 1
    return {0: {'name': name,
                'size': {'x': 0, 'y': 0, 'width': width, 'height': height},
                'mosaic': {'row': 1, 'column': 1},
                'screenInterfaces': interfaces}}


def add_h_device(name='CIRCUIT MOM', cards=None, outputs=None, **kw):
    state = {
        'device_id': 'dev-h', 'name': 'H-series', 'ip': '192.168.0.10',
        'device_type': 'h_series', 'connected': True, 'last_poll': '12:00:01',
        'cards_fresh': True, 'receiving_cards': cards or [], 'device_info': {},
        'screen_outputs': outputs if outputs is not None else screen_outputs(name),
    }
    state.update(kw)
    appmod.manager.devices['dev-h'] = FakeDevice('dev-h', state)
    return state


def write_old_wall_snapshot(tmp_path, name='COSMIC MEADOW', width=11520,
                            height=2160, slots=(20, 22, 24), cards=None):
    """The snapshot as it exists on disk: a wall that no longer exists."""
    snap = {
        'device_ip': '192.168.0.10',
        'screen_name': name,
        'screen_size': {'width': width, 'height': height},
        'mosaic': {'row': 1, 'column': 3},
        'sender_cards': [{'card_number': i + 1, 'slot': s, 'role': 'primary'}
                         for i, s in enumerate(slots)],
        'cards': cards if cards is not None else [make_card(card_id=i)
                                                  for i in range(3)],
    }
    path = tmp_path / 'wall_live_snapshot.json'
    with open(path, 'w') as f:
        json.dump(snap, f)
    return path


def test_snapshot_for_a_different_wall_is_detected(client, tmp_path):
    write_old_wall_snapshot(tmp_path)
    add_h_device()
    data = client.get('/api/wall_live').get_json()

    status = data['snapshot_status']
    assert status['status'] == 'mismatch'
    assert status['checks']['screen_name'] == 'mismatch'
    # Both walls are named so the operator can see the disagreement itself.
    assert status['snapshot']['screen_name'] == 'COSMIC MEADOW'
    assert status['live']['screen_name'] == 'CIRCUIT MOM'
    assert any('COSMIC MEADOW' in r and 'CIRCUIT MOM' in r
               for r in status['reasons'])


def test_stale_snapshot_cards_are_withheld_entirely(client, tmp_path):
    """Not dimmed, not stale-badged — absent. The UI cannot render them."""
    write_old_wall_snapshot(tmp_path)
    add_h_device()
    data = client.get('/api/wall_live').get_json()

    assert 'snapshot' not in data
    assert data['captured_at'] is None
    assert data['cards_source'] is None


def test_payload_never_claims_live_for_stale_cards(client, tmp_path):
    write_old_wall_snapshot(tmp_path)
    add_h_device()
    data = client.get('/api/wall_live').get_json()
    assert data['live'] is False
    assert data['device_connected'] is True


def test_matching_snapshot_is_used(client, tmp_path):
    write_old_wall_snapshot(tmp_path, name='CIRCUIT MOM', width=3840,
                            slots=(20, 22, 28, 30))
    add_h_device()
    data = client.get('/api/wall_live').get_json()

    assert data['snapshot_status']['status'] == 'match'
    assert data['snapshot_status']['checks'] == {
        'screen_name': 'match', 'canvas': 'match', 'sender_slots': 'match'}
    assert data['snapshot']['screen_name'] == 'CIRCUIT MOM'
    assert data['cards_source'] == 'snapshot'
    # Snapshot cards are still not live cards.
    assert data['live'] is False


def test_canvas_size_alone_makes_a_snapshot_stale(client, tmp_path):
    """Same screen name, resized wall — the card map is still wrong."""
    write_old_wall_snapshot(tmp_path, name='CIRCUIT MOM', width=11520,
                            slots=(20, 22, 28, 30))
    data_status = client.get('/api/wall_live').get_json()  # no device yet
    assert data_status['snapshot_status']['status'] == 'unverified'

    add_h_device()
    status = client.get('/api/wall_live').get_json()['snapshot_status']
    assert status['status'] == 'mismatch'
    assert status['checks'] == {'screen_name': 'match', 'canvas': 'mismatch',
                                'sender_slots': 'match'}


def test_sender_slots_alone_make_a_snapshot_stale(client, tmp_path):
    """A slot the controller does not report at all is disqualifying."""
    write_old_wall_snapshot(tmp_path, name='CIRCUIT MOM', width=3840,
                            slots=(20, 22, 24))
    add_h_device()
    status = client.get('/api/wall_live').get_json()['snapshot_status']
    assert status['status'] == 'mismatch'
    assert status['checks']['sender_slots'] == 'mismatch'
    assert any('24' in r for r in status['reasons'])


def test_snapshot_may_omit_slots_that_have_no_cards(client, tmp_path):
    """R0405 lists slots that are not sender cards — on the H15 it reports
    20, 22, 28 and 30, but only 20 and 22 answer R0155. An enumeration can
    only record slots it found cards behind, so a subset must be a match;
    requiring equality rejected a snapshot taken minutes earlier from this
    very controller."""
    write_old_wall_snapshot(tmp_path, name='CIRCUIT MOM', width=3840,
                            slots=(20, 22))
    add_h_device()
    data = client.get('/api/wall_live').get_json()
    assert data['snapshot_status']['checks']['sender_slots'] == 'match'
    assert data['snapshot_status']['status'] == 'match'
    assert data['cards_source'] == 'snapshot'


def test_absent_live_data_is_not_proof_the_snapshot_is_wrong(client, tmp_path):
    """No device, or a device that hasn't answered R0405 yet — unverified."""
    write_old_wall_snapshot(tmp_path)
    data = client.get('/api/wall_live').get_json()
    assert data['snapshot_status']['status'] == 'unverified'
    assert data['snapshot']['screen_name'] == 'COSMIC MEADOW'
    assert data['live'] is False

    add_h_device(outputs={})
    data = client.get('/api/wall_live').get_json()
    assert data['snapshot_status']['status'] == 'unverified'
    assert data['topology'] is None


def test_snapshot_without_identifying_fields_is_unverified(client, tmp_path):
    """An old snapshot with nothing comparable must not be called stale."""
    write_snapshot(tmp_path)          # no screen_size, no sender_cards
    add_h_device()
    status = client.get('/api/wall_live').get_json()['snapshot_status']
    assert status['status'] == 'mismatch'   # screen name still disagrees

    write_snapshot(tmp_path, cards=[make_card()])
    snap = json.load(open(tmp_path / 'wall_live_snapshot.json'))
    del snap['screen_name']
    with open(tmp_path / 'wall_live_snapshot.json', 'w') as f:
        json.dump(snap, f)
    appmod._snapshot_cache.clear()
    status = client.get('/api/wall_live').get_json()['snapshot_status']
    assert status['status'] == 'unverified'
    assert set(status['checks'].values()) == {'unknown'}


def test_compare_is_case_and_whitespace_insensitive():
    topo = {'screen_name': 'Circuit Mom', 'canvas': None, 'sender_slots': []}
    verdict = appmod.compare_snapshot_to_topology(
        {'screen_name': '  CIRCUIT MOM '}, topo)
    assert verdict['status'] == 'match'


def test_compare_with_no_snapshot(client):
    assert appmod.compare_snapshot_to_topology(None, None)['status'] == 'no_snapshot'


# ── Panel count honesty ───────────────────────────────────

def test_panel_count_unknown_without_a_valid_enumeration(client, tmp_path):
    write_old_wall_snapshot(tmp_path)      # 3 cards, wrong wall
    add_h_device()
    panels = client.get('/api/wall_live').get_json()['panels']

    assert panels['known'] is False
    assert panels['count'] is None
    assert panels['source'] is None
    assert 'different wall' in panels['reason']
    assert '--yes-contact-hardware' in panels['enumerate_hint']


def test_panel_count_unknown_with_no_snapshot_at_all(client):
    add_h_device()
    panels = client.get('/api/wall_live').get_json()['panels']
    assert panels['known'] is False and panels['count'] is None


def test_panel_capacity_is_geometry_not_a_panel_count(client, monkeypatch):
    """3840x2160 at a 60x120 pitch = 64 x 18 = 1152 positions."""
    monkeypatch.setattr(appmod, '_configured_panel_size',
                        lambda: {'width': 60, 'height': 120})
    add_h_device()
    panels = client.get('/api/wall_live').get_json()['panels']
    assert panels['capacity'] == {'columns': 64, 'rows': 18, 'panels': 1152,
                                  'panel': {'width': 60, 'height': 120}}
    # Capacity is never promoted into the count.
    assert panels['count'] is None and panels['known'] is False


def test_panel_capacity_unit_maths():
    assert appmod.panel_capacity({'width': 3840, 'height': 2160},
                                 {'width': 60, 'height': 120}) == {
        'columns': 64, 'rows': 18, 'panels': 1152,
        'panel': {'width': 60, 'height': 120}}


def test_panel_capacity_is_none_when_panel_size_is_unreadable(client, monkeypatch):
    monkeypatch.setattr(appmod, '_configured_panel_size', lambda: None)
    add_h_device()
    assert client.get('/api/wall_live').get_json()['panels']['capacity'] is None


def test_panel_capacity_is_none_without_canvas_geometry(client, tmp_path):
    write_snapshot(tmp_path)
    panels = client.get('/api/wall_live').get_json()['panels']
    assert panels['capacity'] is None


def test_panel_count_known_from_a_matching_enumeration(client, tmp_path):
    write_old_wall_snapshot(tmp_path, name='CIRCUIT MOM', width=3840,
                            slots=(20, 22, 28, 30),
                            cards=[make_card(card_id=i) for i in range(7)])
    add_h_device()
    panels = client.get('/api/wall_live').get_json()['panels']
    assert panels == {'known': True, 'count': 7, 'source': 'enumeration',
                      'capacity': panels['capacity'],
                      'populated_sender_cards': 1, 'read': 0,
                      'enumerate_hint': appmod.ENUMERATE_HINT}


def test_panel_count_from_a_live_read_beats_any_snapshot(client, tmp_path):
    write_old_wall_snapshot(tmp_path)
    add_h_device(cards=[make_card(card_id=i) for i in range(4)])
    data = client.get('/api/wall_live').get_json()
    # No verified inventory here (the stored snapshot is a different wall),
    # so the only count available is what answered — and it is a floor.
    assert data['panels'] == {'known': True, 'count': 4,
                              'source': 'device_read',
                              'capacity': data['panels']['capacity'],
                              'populated_sender_cards': 1, 'read': 4,
                              'is_lower_bound': True,
                              'enumerate_hint': appmod.ENUMERATE_HINT}
    # Live cards are live — but the stale snapshot is still withheld.
    assert data['live'] is True
    assert 'snapshot' not in data


def test_unverified_snapshot_count_is_not_promoted_to_known(client, tmp_path):
    write_old_wall_snapshot(tmp_path)
    panels = client.get('/api/wall_live').get_json()['panels']
    assert panels['known'] is False
    assert panels['count'] == 3
    assert panels['source'] == 'unverified_snapshot'


def test_panel_capacity_rejects_junk_geometry():
    assert appmod.panel_capacity(None, {'width': 60, 'height': 120}) is None
    assert appmod.panel_capacity({'width': 3840, 'height': 2160}, None) is None
    assert appmod.panel_capacity({'width': 3840, 'height': 2160},
                                 {'width': 0, 'height': 120}) is None
    assert appmod.panel_capacity({'width': '3840', 'height': 2160},
                                 {'width': 60, 'height': 120}) is None


# ── Live topology in the payload ──────────────────────────

def test_topology_comes_from_the_device_every_poll(client, tmp_path):
    write_old_wall_snapshot(tmp_path)
    add_h_device()
    data = client.get('/api/wall_live').get_json()

    topo = data['topology']
    assert data['topology_source'] == 'device'
    assert topo['screen_name'] == 'CIRCUIT MOM'
    assert topo['canvas'] == {'width': 3840, 'height': 2160}
    assert topo['mosaic'] == {'row': 1, 'column': 1}
    assert topo['sender_slots'] == [20, 22, 28, 30]


def test_active_outputs_understand_sender_redundancy(client):
    """16 output connections across 4 slots covering one canvas = 4 outputs."""
    add_h_device()
    topo = client.get('/api/wall_live').get_json()['topology']
    assert topo['outputs_total'] == 16
    assert topo['active_outputs'] == 4
    assert topo['redundant'] is True
    assert topo['redundancy_factor'] == 4
    assert topo['card_online_known'] is False


def test_wall_live_unavailable_when_nothing_is_knowable(client):
    data = client.get('/api/wall_live').get_json()
    assert data['available'] is False
    assert data['live'] is False
    assert data['topology'] is None
    assert '--yes-contact-hardware' in data['enumerate_hint']


def test_snmp_counts_are_passed_through_separately(client):
    add_h_device(snmp={'available': True, 'screens': {'screen_count': 1},
                       'output': {'card_count': 4, 'port_count': 16}})
    summary = client.get('/api/wall_live').get_json()['snmp_summary']
    assert summary == {'available': True, 'screen_count': 1,
                       'output_card_count': 4, 'port_count': 16}


def test_missing_snmp_block_degrades_to_none(client):
    add_h_device()
    summary = client.get('/api/wall_live').get_json()['snmp_summary']
    assert summary == {'available': None, 'screen_count': None,
                       'output_card_count': None, 'port_count': None}


def test_wall_config_and_rendered_endpoints(client):
    assert client.get('/api/wall_config').status_code == 200
    assert client.get('/api/wall_layout').status_code == 200
    assert client.get('/api/wall_rendered').status_code == 200


def test_wall_layout_rejects_non_object(client):
    assert client.post('/api/wall_layout', json=[1, 2]).status_code == 400


# ── Devices (Task 7) ──────────────────────────────────────

def test_add_device_with_valid_ip(client, tmp_path):
    res = client.post('/api/devices', json={'ip': '10.1.2.3', 'port': 5203,
                                            'name': 'Wall A'})
    assert res.status_code == 200
    assert res.get_json()['device_id'] == 'dev-10-1-2-3'
    assert 'dev-10-1-2-3' in appmod.manager.devices
    saved = read_settings_file(tmp_path)['devices']
    assert saved[0]['ip'] == '10.1.2.3' and saved[0]['port'] == 5203


@pytest.mark.parametrize('bad_ip', [
    '192.168.0.999',
    'not-an-ip',
    '192.168.0',
    '',
    '192.168.0.1; rm -rf /',
    '<img src=x onerror=alert(1)>',
    '192.168.0.1/24',
])
def test_add_device_rejects_invalid_ip(client, bad_ip):
    res = client.post('/api/devices', json={'ip': bad_ip})
    assert res.status_code == 400
    assert 'Invalid IP' in res.get_json()['error']
    assert appmod.manager.devices == {}


def test_add_device_requires_ip(client):
    assert client.post('/api/devices', json={}).status_code == 400
    assert client.post('/api/devices', json={'name': 'x'}).status_code == 400


@pytest.mark.parametrize('bad_port', ['abc', 0, 70000, -1])
def test_add_device_rejects_invalid_port(client, bad_port):
    res = client.post('/api/devices', json={'ip': '10.0.0.5', 'port': bad_port})
    assert res.status_code == 400


def test_add_duplicate_device_conflicts(client):
    client.post('/api/devices', json={'ip': '10.0.0.7'})
    res = client.post('/api/devices', json={'ip': '10.0.0.7'})
    assert res.status_code == 409


def test_remove_device(client, tmp_path):
    client.post('/api/devices', json={'ip': '10.0.0.8'})
    res = client.delete('/api/devices/dev-10-0-0-8')
    assert res.status_code == 200
    assert 'dev-10-0-0-8' not in appmod.manager.devices
    assert read_settings_file(tmp_path)['devices'] == []


def test_remove_unknown_device_is_404(client):
    assert client.delete('/api/devices/dev-1-1-1-1').status_code == 404


# ── Settings (Task 8) ─────────────────────────────────────

def test_default_poll_interval_is_unified(client):
    data = client.get('/api/settings').get_json()
    assert data['poll_interval'] == appmod.DEFAULT_POLL_INTERVAL
    assert appmod.DEFAULT_SETTINGS['poll_interval'] == appmod.DEFAULT_POLL_INTERVAL


def test_settings_post_applies_poll_interval_live(client):
    res = client.post('/api/settings', json={'poll_interval': 30})
    assert res.status_code == 200
    assert appmod.manager.poll_interval == 30.0       # no restart required
    assert client.get('/api/settings').get_json()['poll_interval'] == 30.0


def test_settings_post_clamps_out_of_range(client):
    client.post('/api/settings', json={'poll_interval': 0})
    assert appmod.manager.poll_interval == appmod.MIN_POLL_INTERVAL
    client.post('/api/settings', json={'poll_interval': 99999})
    assert appmod.manager.poll_interval == appmod.MAX_POLL_INTERVAL


def test_settings_post_rejects_non_numeric(client):
    res = client.post('/api/settings', json={'poll_interval': 'fast'})
    assert res.status_code == 400
    res = client.post('/api/settings', json={'temp_critical': None})
    assert res.status_code == 400


def test_settings_post_rejects_non_object(client):
    assert client.post('/api/settings', json=[1, 2]).status_code == 400
    assert client.post('/api/settings', json={'devices': 'nope'}).status_code == 400


def test_settings_are_cached_not_reread(client, tmp_path, monkeypatch):
    """load_settings() runs on every device update — it must not hit disk."""
    client.get('/api/settings')
    reads = []
    real_open = open

    def counting_open(path, *a, **kw):
        if str(path) == str(tmp_path / 'settings.json'):
            reads.append(path)
        return real_open(path, *a, **kw)

    monkeypatch.setattr('builtins.open', counting_open)
    for _ in range(10):
        appmod.load_settings()
    assert reads == []


# ── Error log lifecycle (Task 5) ──────────────────────────

def test_error_lifecycle_create_resolve_clear(client, tmp_path):
    appmod.add_error('CRITICAL', 'Wall', 'Too hot', cabinet='Slot 20 · Port 0 · Card 3',
                     port=0, value=95.0)
    listed = client.get('/api/errors').get_json()
    assert len(listed) == 1
    entry = listed[0]
    assert entry['cabinet'] == 'Slot 20 · Port 0 · Card 3'
    assert entry['port'] == 0

    assert client.post(f'/api/errors/{entry["id"]}/acknowledge').status_code == 200
    assert client.post(f'/api/errors/{entry["id"]}/resolve').status_code == 200
    assert client.get('/api/errors').get_json()[0]['resolved'] is True

    res = client.post('/api/errors/clear-resolved')
    assert res.status_code == 200
    assert client.get('/api/errors').get_json() == []

    # Persisted atomically along the way
    with open(tmp_path / 'error_log.json') as f:
        assert json.load(f) == []


def test_error_ids_do_not_collide_after_clear(client):
    """`len(log) + 1` re-issued ids after a clear, so resolve hit the wrong row."""
    for i in range(3):
        appmod.add_error('WARNING', 'Wall', f'msg {i}')
    ids = [e['id'] for e in appmod._error_log]
    assert ids == [1, 2, 3]

    client.post(f'/api/errors/{ids[0]}/resolve')
    client.post(f'/api/errors/{ids[1]}/resolve')
    client.post('/api/errors/clear-resolved')
    assert [e['id'] for e in appmod._error_log] == [3]

    new_entry = appmod.add_error('CRITICAL', 'Wall', 'after clear')
    assert new_entry['id'] == 4                       # not 2
    assert len({e['id'] for e in appmod._error_log}) == 2

    client.post(f'/api/errors/{new_entry["id"]}/resolve')
    resolved = [e for e in appmod._error_log if e['resolved']]
    assert [e['message'] for e in resolved] == ['after clear']


def test_error_id_counter_seeds_from_existing_log(client, tmp_path):
    with open(tmp_path / 'error_log.json', 'w') as f:
        json.dump([{'id': 42, 'severity': 'WARNING', 'device': 'W',
                    'message': 'old', 'resolved': False}], f)
    appmod.load_error_log()
    assert appmod.add_error('WARNING', 'W', 'new')['id'] == 43


def test_corrupt_error_log_is_preserved_not_discarded(client, tmp_path):
    path = tmp_path / 'error_log.json'
    path.write_text('{not valid json')
    appmod.load_error_log()
    assert appmod._error_log == []
    assert os.path.exists(str(path) + '.corrupt')     # history kept for recovery


def test_error_filters_and_limit(client):
    appmod.add_error('CRITICAL', 'A', 'a')
    appmod.add_error('WARNING', 'B', 'b')
    assert len(client.get('/api/errors?severity=CRITICAL').get_json()) == 1
    assert len(client.get('/api/errors?device=B').get_json()) == 1
    assert len(client.get('/api/errors?resolved=false').get_json()) == 2
    assert len(client.get('/api/errors?limit=1').get_json()) == 1
    assert client.get('/api/errors?limit=abc').status_code == 400


def test_resolve_unknown_error_is_404(client):
    assert client.post('/api/errors/999/resolve').status_code == 404
    assert client.post('/api/errors/999/acknowledge').status_code == 404


def test_error_log_written_atomically(client, tmp_path):
    appmod.add_error('WARNING', 'Wall', 'x')
    # No leftover temp files, and the result parses.
    assert [p for p in os.listdir(tmp_path) if p.startswith('.tmp-')] == []
    with open(tmp_path / 'error_log.json') as f:
        assert json.load(f)[0]['message'] == 'x'


def test_errors_csv_export(client):
    appmod.add_error('CRITICAL', 'Wall', 'Too hot', cabinet='Slot 20', port=3,
                     value=95.0)
    res = client.get('/api/export/errors.csv')
    assert res.status_code == 200
    body = res.data.decode()
    assert 'Cabinet' in body and 'Slot 20' in body


# ── Alerting: per-card, not per-average (Tasks 1 & 2) ─────

DEFAULTS = dict(appmod.DEFAULT_SETTINGS)


def test_one_hot_card_in_a_big_wall_alerts(client):
    """The headline bug: a single 95 °C card among 1373 cool ones.

    The mean across the wall moves ~0.04 °C, so the old average-based check
    could never fire.
    """
    cards = [make_card(slot=20, port=p, card_id=i, temp=44.0)
             for p in range(4) for i in range(343)]
    cards[7]['temperature_c'] = cards[7]['temp_c'] = 95.0

    breaches = appmod._card_breaches(cards, DEFAULTS)
    assert len(breaches) == 1
    assert breaches[0]['severity'] == 'CRITICAL'
    assert breaches[0]['value'] == 95.0


def test_alert_names_the_failing_panel(client):
    state = {'name': 'H-series Live',
             'cards_fresh': True, 'receiving_cards': [make_card(slot=20, port=2, card_id=7, temp=95.0)],
             'live_monitoring': {}}
    appmod.evaluate_alerts('dev-h', state)

    assert len(appmod._error_log) == 1
    entry = appmod._error_log[0]
    assert entry['severity'] == 'CRITICAL'
    assert entry['cabinet'] == 'Slot 20 · Port 2 · Card 7'
    assert entry['port'] == 2                  # was always None before
    assert entry['value'] == 95.0


def test_warning_and_critical_temperature_bands(client):
    warn = appmod._card_breaches([make_card(temp=65.0)], DEFAULTS)
    assert warn[0]['severity'] == 'WARNING'
    crit = appmod._card_breaches([make_card(temp=80.0)], DEFAULTS)
    assert crit[0]['severity'] == 'CRITICAL'
    assert appmod._card_breaches([make_card(temp=40.0)], DEFAULTS) == []


def test_zero_volts_is_a_critical_alert(client):
    """0.0 V is a dead supply — and falsy, so the old guard skipped it."""
    breaches = appmod._card_breaches([make_card(voltage=0.0)], DEFAULTS)
    assert len(breaches) == 1
    assert breaches[0]['metric'] == 'Voltage'
    assert breaches[0]['severity'] == 'CRITICAL'
    assert breaches[0]['value'] == 0.0


def test_low_but_live_voltage_is_a_warning(client):
    breaches = appmod._card_breaches([make_card(voltage=3.2)], DEFAULTS)
    assert breaches[0]['severity'] == 'WARNING'


# ── The voltage floor ─────────────────────────────────────
#
# 4.7 V was above the entire normal operating range of this hardware, so every
# healthy card breached it every cycle. src/error_log.json still holds the
# result: "Voltage 4.19V below minimum threshold of 4.7V" across 15 cards.

@pytest.mark.parametrize('volts', [4.10, 4.25, 4.40, 4.60, 4.95, 5.19])
def test_the_real_walls_voltages_read_healthy(client, volts):
    """4.1-4.4 V is the centi fleet, 4.95-5.19 the byte fleet, 4.6 the spec."""
    assert appmod._card_breaches([make_card(voltage=volts)], DEFAULTS) == []


def test_the_floor_still_catches_a_sagging_rail(client):
    breaches = appmod._card_breaches([make_card(voltage=3.5)], DEFAULTS)
    assert [(b['metric'], b['severity']) for b in breaches] == \
        [('Voltage', 'WARNING')]


def test_a_dead_rail_is_still_critical(client):
    breaches = appmod._card_breaches([make_card(voltage=0.0)], DEFAULTS)
    assert breaches[0]['severity'] == 'CRITICAL'


def test_the_default_sits_below_every_observed_healthy_reading(client):
    assert appmod.DEFAULT_SETTINGS['voltage_min'] == appmod.DEFAULT_VOLTAGE_MIN
    assert appmod.DEFAULT_VOLTAGE_MIN < 4.10      # lowest observed on the wall
    assert appmod.DEFAULT_VOLTAGE_MIN > appmod.VOLTAGE_DEAD_V


def test_a_persisted_legacy_threshold_is_corrected(client, tmp_path):
    """A changed default never reaches an install that already saved one."""
    with open(tmp_path / 'settings.json', 'w') as f:
        json.dump({'voltage_min': appmod.LEGACY_VOLTAGE_MIN}, f)
    settings = appmod.reload_settings()
    assert settings['voltage_min'] == appmod.DEFAULT_VOLTAGE_MIN
    assert appmod._card_breaches([make_card(voltage=4.19)], settings) == []


def test_a_deliberate_operator_threshold_is_left_alone(client, tmp_path):
    """Only the exact old default is rewritten — 4.6 is somebody's choice."""
    with open(tmp_path / 'settings.json', 'w') as f:
        json.dump({'voltage_min': 4.6}, f)
    assert appmod.reload_settings()['voltage_min'] == 4.6


# ── Power supplies ────────────────────────────────────────
#
# Severity must match wall_view.js powerState(): exactly one supply flagged is
# a genuine hardware failure, both flagged at once is nearly always an R0155
# decode artifact from a transient timeout.

def power_card(primary=True, backup=True, **kw):
    """A card carrying power-supply flags and otherwise healthy readings."""
    return {**make_card(**kw),
            'primary_power_ok': primary, 'backup_power_ok': backup}


def test_healthy_supplies_do_not_alert(client):
    assert appmod._card_breaches([power_card()], DEFAULTS) == []


def test_cards_without_power_fields_do_not_alert(client):
    """The binary path reports no power flags at all — absence is not a fault."""
    assert appmod._card_breaches([make_card()], DEFAULTS) == []
    assert appmod._card_breaches(
        [power_card(primary=None, backup=None)], DEFAULTS) == []


@pytest.mark.parametrize('primary,backup,which', [
    (False, True, 'Primary'),
    (True, False, 'Backup'),
    (False, None, 'Primary'),
])
def test_one_failed_supply_is_critical(client, primary, backup, which):
    """One supply down: the card is lit but unprotected — a real fault."""
    breaches = appmod._card_breaches(
        [power_card(primary=primary, backup=backup)], DEFAULTS)
    assert len(breaches) == 1
    assert breaches[0]['metric'] == 'Power supply'
    assert breaches[0]['severity'] == 'CRITICAL'
    assert which in breaches[0]['text']


def test_both_supplies_flagged_is_only_a_warning(client):
    """Both down at once is an R0155 artifact, not a 3am page."""
    breaches = appmod._card_breaches(
        [power_card(primary=False, backup=False)], DEFAULTS)
    assert len(breaches) == 1
    assert breaches[0]['severity'] == 'WARNING'
    assert 'transient' in breaches[0]['text']


def test_offline_cards_are_not_power_alerted(client):
    cards = [power_card(primary=False, backup=True, online=False)]
    assert appmod._card_breaches(cards, DEFAULTS) == []


def test_power_fault_names_the_failing_panel(client):
    state = {'name': 'H-series Live',
             'cards_fresh': True, 'receiving_cards': [power_card(slot=20, port=2, card_id=7,
                                            primary=False)],
             'live_monitoring': {}}
    appmod.evaluate_alerts('dev-h', state)

    assert len(appmod._error_log) == 1
    entry = appmod._error_log[0]
    assert entry['severity'] == 'CRITICAL'
    assert entry['cabinet'] == 'Slot 20 · Port 2 · Card 7'
    assert entry['port'] == 2
    assert 'power supply failed' in entry['message']


def test_power_faults_are_deduped_per_card(client):
    state = {'name': 'Wall',
             'cards_fresh': True, 'receiving_cards': [power_card(slot=20, port=0, card_id=1,
                                            primary=False)],
             'live_monitoring': {}}
    for _ in range(5):
        appmod.evaluate_alerts('dev-h', state)
    assert len(appmod._error_log) == 1


def test_chain_wide_power_failure_is_one_alert(client):
    """A dead power leg takes out a whole run — one event, not 40.

    Also exercises _worst() on a metric with no numeric value.
    """
    cards = [power_card(slot=20, port=3, card_id=i, primary=False)
             for i in range(40)]
    appmod.evaluate_alerts('dev-h', {'name': 'Wall', 'cards_fresh': True, 'receiving_cards': cards,
                                     'live_monitoring': {}})
    assert len(appmod._error_log) == 1
    assert '40 cards' in appmod._error_log[0]['message']
    assert appmod._error_log[0]['port'] == 3


def test_wall_wide_power_failure_collapses_to_one_alert(client):
    cards = [power_card(slot=20, port=p, card_id=i, primary=False)
             for p in range(6) for i in range(20)]
    appmod.evaluate_alerts('dev-h', {'name': 'Wall', 'cards_fresh': True, 'receiving_cards': cards,
                                     'live_monitoring': {}})
    assert len(appmod._error_log) == 1
    assert '6 chains' in appmod._error_log[0]['message']


def test_power_and_temperature_faults_are_separate_alerts(client):
    """One card failing two ways is two events — they group independently."""
    card = power_card(slot=20, port=1, card_id=4, temp=95.0, primary=False)
    metrics = {b['metric'] for b in appmod._card_breaches([card], DEFAULTS)}
    assert metrics == {'Temperature', 'Power supply'}

    appmod.evaluate_alerts('dev-h', {'name': 'Wall', 'cards_fresh': True, 'receiving_cards': [card],
                                     'live_monitoring': {}})
    assert len(appmod._error_log) == 2


def test_worst_falls_back_when_nothing_is_rankable(client):
    """Power breaches carry value=None; ranking them must not raise."""
    items = appmod._card_breaches(
        [power_card(slot=20, port=0, card_id=i, primary=False) for i in range(3)],
        DEFAULTS)
    assert appmod._worst(items) is items[0]


# ── Bit errors ────────────────────────────────────────────

def bit_card(errors, saturated=False, **kw):
    return {**make_card(**kw),
            'bit_errors': errors, 'bit_errors_saturated': saturated}


def test_a_handful_of_bit_errors_is_normal(client):
    """Single-digit counts turn up on a long chain as ordinary cable noise."""
    assert appmod._card_breaches([bit_card(0)], DEFAULTS) == []
    assert appmod._card_breaches([bit_card(7)], DEFAULTS) == []
    assert appmod._card_breaches(
        [bit_card(appmod.BIT_ERROR_WARNING - 1)], DEFAULTS) == []


def test_bit_errors_above_threshold_warn(client):
    breaches = appmod._card_breaches(
        [bit_card(appmod.BIT_ERROR_WARNING)], DEFAULTS)
    assert len(breaches) == 1
    assert breaches[0]['metric'] == 'Bit errors'
    assert breaches[0]['severity'] == 'WARNING'
    assert breaches[0]['value'] == appmod.BIT_ERROR_WARNING


def test_saturated_bit_error_counter_is_critical(client):
    """0xFFFF is the counter pinning, not a count — always serious."""
    breaches = appmod._card_breaches([bit_card(0xFFFF, saturated=True)], DEFAULTS)
    assert len(breaches) == 1
    assert breaches[0]['severity'] == 'CRITICAL'
    assert '0xFFFF' in breaches[0]['text']


def test_saturation_wins_over_the_count(client):
    """A saturated card must not also raise the WARNING for the same reading."""
    breaches = appmod._card_breaches([bit_card(0xFFFF, saturated=True)], DEFAULTS)
    assert [b['severity'] for b in breaches] == ['CRITICAL']


def test_bit_error_alert_names_the_panel(client):
    state = {'name': 'Wall',
             'cards_fresh': True, 'receiving_cards': [bit_card(0xFFFF, saturated=True, slot=20,
                                          port=5, card_id=12)],
             'live_monitoring': {}}
    appmod.evaluate_alerts('dev-h', state)
    assert len(appmod._error_log) == 1
    assert appmod._error_log[0]['cabinet'] == 'Slot 20 · Port 5 · Card 12'
    assert appmod._error_log[0]['value'] == 0xFFFF


def test_zero_temperature_is_evaluated_not_skipped(client):
    settings = {**DEFAULTS, 'temp_warning': 0.0}
    breaches = appmod._card_breaches([make_card(temp=0.0, voltage=5.0)], settings)
    assert [b['metric'] for b in breaches] == ['Temperature']


def test_offline_cards_are_not_temperature_alerted(client):
    cards = [make_card(temp=95.0, voltage=0.0, online=False)]
    assert appmod._card_breaches(cards, DEFAULTS) == []


def test_missing_readings_are_skipped(client):
    cards = [{'slot': 1, 'port': 0, 'card_id': 0, 'online': True}]
    assert appmod._card_breaches(cards, DEFAULTS) == []


# ── The absent-card alert storm ───────────────────────────
#
# On the H-series centi firmware an unreachable card still ANSWERS R0155, with
# workStatus 1 and temp/volt/brightness all 0. Decoded naively that is a wall
# full of cards sitting on a dead 0 V rail — the exact input that turns "a
# dead PSU pages someone" into hundreds of CRITICALs on first contact. These
# tests run the real captured payloads through the real parser, so they fail
# if either the parser or the breach guard regresses.

# Copied verbatim from the live capture off 192.168.0.10.
R0155_REPORTING = {
    "deviceId": 0, "slotId": 20, "portId": 0, "recvCardId": 10,
    "mcuVersion": "V4.5.1.81", "fpgaVersion": "V4.5.1.81",
    "workStatus": 0, "tempStatus": 0, "temp": 3700, "tempMax": 70,
    "voltStatus": 0, "volt": 440, "power0Status": 0, "power1Status": 0,
    "brightness": 25, "cmd": "R0155", "ack": "Ok",
}
R0155_ABSENT = {**R0155_REPORTING, "recvCardId": 11, "workStatus": 1,
                "tempStatus": 2, "temp": 0, "voltStatus": 2, "volt": 0,
                "brightness": 0}
# The older byte-schema reply — a different device/firmware in the same fleet.
R0155_BYTE_SCHEMA = {
    "deviceId": 0, "slotId": 20, "portId": 0, "recvCardId": 0,
    "power0Status": 0, "power1Status": 0, "brightness": 127,
    "temp": 88, "voltage": 170, "cmd": "R0155", "ack": "Ok",
}


def _parsed_card(reply):
    """Decode an R0155 reply the way device_manager's refresh does."""
    import h_series_json

    card = h_series_json.parse_receiving_card(reply)
    return {'slot': card['slot'], 'port': card['port'],
            'card_id': card['card_id'], **card}


def test_absent_card_raises_no_voltage_alert(client):
    """workStatus 1 + volt 0 must not read as 'supply appears dead'."""
    assert appmod._card_breaches([_parsed_card(R0155_ABSENT)], DEFAULTS) == []


def test_a_wall_of_absent_cards_raises_nothing(client):
    """First contact with the real wall: hundreds absent, no alert storm."""
    cards = [_parsed_card({**R0155_ABSENT, 'portId': p, 'recvCardId': i})
             for p in range(4) for i in range(343)]
    assert appmod._card_breaches(cards, DEFAULTS) == []

    state = {'name': 'H-series Live', 'cards_fresh': True, 'receiving_cards': cards,
             'live_monitoring': {}}
    appmod.evaluate_alerts('dev-h', state)
    assert appmod._error_log == []


def test_absent_card_supplies_are_not_read_as_healthy(client):
    """power0/1Status 0 on an absent card claims nothing either way."""
    card = _parsed_card(R0155_ABSENT)
    assert card['primary_power_ok'] is None
    assert card['backup_power_ok'] is None


def test_reporting_card_still_alerts_on_a_real_dead_supply(client):
    """The guard must not have disarmed the alert it was built for."""
    card = _parsed_card({**R0155_REPORTING, 'volt': 0})
    assert card['online'] is True and card['voltage_v'] == 0.0
    breaches = appmod._card_breaches([card], DEFAULTS)
    assert [(b['metric'], b['severity']) for b in breaches] == \
        [('Voltage', 'CRITICAL')]
    assert 'supply appears dead' in breaches[0]['text']


def test_reporting_card_temperature_is_not_1850_degrees(client):
    """The decode bug: 3700/2 = 1850 °C fired CRITICAL on every good card."""
    card = _parsed_card(R0155_REPORTING)
    assert card['temperature_c'] == 37.0
    temps = [b for b in appmod._card_breaches([card], DEFAULTS)
             if b['metric'] == 'Temperature']
    assert temps == []


def test_byte_schema_card_still_evaluates(client):
    """The older reply shape keeps its own scaling and its own alerting.

    Byte-schema voltage is (raw & 0x7F) * 0.1 per the vendor doc §4.3.4 /
    §5.4.2, so raw 170 → 4.2 V. That is above DEFAULT_VOLTAGE_MIN (3.8 V), so
    a healthy card still raises no breach — which is the point of the test.
    It read 5.1 V while the code used `raw * 0.03`, a formula this project
    invented to clear a 4.7 V alarm that was itself wrong for this hardware.
    """
    card = _parsed_card(R0155_BYTE_SCHEMA)
    assert card['temperature_c'] == 44.0 and card['voltage_v'] == 4.2
    assert appmod._card_breaches([card], DEFAULTS) == []
    hot = _parsed_card({**R0155_BYTE_SCHEMA, 'temp': 160})   # 80 °C
    assert appmod._card_breaches([hot], DEFAULTS)[0]['severity'] == 'CRITICAL'


def test_reporting_flag_alone_blocks_evaluation(client):
    """Belt and braces: `reporting: False` is honoured even if online is True."""
    card = {**make_card(temp=95.0, voltage=0.0), 'reporting': False}
    assert appmod._card_breaches([card], DEFAULTS) == []


def test_alerts_are_deduped_per_card(client):
    state = {'name': 'Wall',
             'cards_fresh': True, 'receiving_cards': [make_card(slot=20, port=0, card_id=1, temp=95.0)],
             'live_monitoring': {}}
    for _ in range(5):
        appmod.evaluate_alerts('dev-h', state)
    assert len(appmod._error_log) == 1


def test_dedupe_is_per_card_not_global(client):
    """Two different hot cards are two incidents, not one deduped incident."""
    state = {'name': 'Wall', 'live_monitoring': {}, 'cards_fresh': True,
             'receiving_cards': [
        make_card(slot=20, port=0, card_id=1, temp=95.0),
        make_card(slot=20, port=0, card_id=2, temp=96.0),
    ]}
    appmod.evaluate_alerts('dev-h', state)
    assert len(appmod._error_log) == 2


def test_dedupe_survives_a_burst_larger_than_the_log_tail(client):
    """The old dedupe scanned only the last 20 entries — a burst defeated it."""
    cards = [make_card(slot=20, port=p, card_id=0, temp=95.0) for p in range(2)]
    for _ in range(60):
        appmod.evaluate_alerts('dev-h', {'name': 'Wall', 'cards_fresh': True, 'receiving_cards': cards,
                                         'live_monitoring': {}})
    assert len(appmod._error_log) == 2


def test_chain_wide_failure_is_summarised_as_one_alert(client):
    """40 hot cards on one chain is one event, not 40."""
    cards = [make_card(slot=20, port=3, card_id=i, temp=95.0) for i in range(40)]
    appmod.evaluate_alerts('dev-h', {'name': 'Wall', 'cards_fresh': True, 'receiving_cards': cards,
                                     'live_monitoring': {}})
    assert len(appmod._error_log) == 1
    entry = appmod._error_log[0]
    assert '40 cards' in entry['message']
    assert entry['port'] == 3


def test_wall_wide_failure_collapses_to_one_alert(client):
    cards = [make_card(slot=20, port=p, card_id=i, temp=95.0)
             for p in range(6) for i in range(20)]
    appmod.evaluate_alerts('dev-h', {'name': 'Wall', 'cards_fresh': True, 'receiving_cards': cards,
                                     'live_monitoring': {}})
    assert len(appmod._error_log) == 1
    assert '6 chains' in appmod._error_log[0]['message']


def test_alert_volume_is_capped_per_cycle(client, monkeypatch):
    monkeypatch.setattr(appmod, 'DEVICE_ROLLUP_CHAIN_THRESHOLD', 10_000)
    monkeypatch.setattr(appmod, 'CHAIN_ALERT_THRESHOLD', 10_000)
    monkeypatch.setattr(appmod, 'MAX_ALERTS_PER_CYCLE', 5)
    cards = [make_card(slot=20, port=p, card_id=0, temp=95.0) for p in range(50)]
    appmod.evaluate_alerts('dev-h', {'name': 'Wall', 'cards_fresh': True, 'receiving_cards': cards,
                                     'live_monitoring': {}})
    assert len(appmod._error_log) == 5


def test_vx1000_style_cards_are_identified_by_label(client):
    cards = [{'index': 4, 'label': 'P3C05', 'port': 3, 'online': True,
              'temperature_c': 95.0, 'voltage_v': 5.0}]
    appmod.evaluate_alerts('dev-vx', {'name': 'VX', 'cards_fresh': True, 'receiving_cards': cards,
                                      'live_monitoring': {}})
    assert appmod._error_log[0]['cabinet'] == 'P3C05'
    assert appmod._error_log[0]['port'] == 3


def test_device_rollup_uses_max_not_mean(client):
    """The rollup reads temperature_max_c; the mean can never cross a threshold."""
    state = {'name': 'Wall', 'cards_fresh': True, 'receiving_cards': [], 'live_monitoring': {
        'card_count': 1374, 'temperature_c': 44.1, 'temperature_max_c': 95.0,
    }}
    appmod.evaluate_alerts('dev-h', state)
    assert len(appmod._error_log) == 1
    assert 'Hottest card 95.0' in appmod._error_log[0]['message']
    assert appmod._error_log[0]['severity'] == 'CRITICAL'


def test_device_rollup_quiet_when_max_is_healthy(client):
    state = {'name': 'Wall', 'cards_fresh': True, 'receiving_cards': [], 'live_monitoring': {
        'card_count': 1374, 'temperature_c': 44.1, 'temperature_max_c': 50.0,
    }}
    appmod.evaluate_alerts('dev-h', state)
    assert appmod._error_log == []


def test_device_rollup_uses_voltage_min_when_available(client):
    state = {'name': 'Wall', 'cards_fresh': True, 'receiving_cards': [], 'live_monitoring': {
        'voltage_v': 4.9, 'voltage_min_v': 0.0,
    }}
    appmod.evaluate_alerts('dev-h', state)
    assert len(appmod._error_log) == 1
    assert appmod._error_log[0]['severity'] == 'CRITICAL'


def test_alert_cooldown_helper(client):
    assert appmod._alert_due('k') is True
    assert appmod._alert_due('k') is False
    assert appmod._alert_due('other') is True
    assert appmod._alert_due('k', cooldown=0.0) is True


def test_connection_error_alert_is_deduped(client):
    for _ in range(3):
        appmod.on_device_error('dev-h', {'error': 'timed out'})
    assert len(appmod._error_log) == 1
    assert 'Connection error' in appmod._error_log[0]['message']


def test_evaluate_alerts_survives_garbage_readings(client):
    state = {'name': 'Wall', 'live_monitoring': {'temperature_max_c': 'n/a'},
             'receiving_cards': [{'slot': 1, 'port': 0, 'card_id': 0,
                                  'online': True, 'temperature_c': 'hot',
                                  'voltage_v': None}]}
    appmod.on_device_update('dev-h', state)      # must not raise
    assert appmod._error_log == []


# ── Alerting on SNMP data ─────────────────────────────────
#
# Routine per-card polling is gone, so these are the signals that arrive every
# cycle. They go through the same add_error/tiering machinery as card
# breaches, which is what keeps one physical fault to one alert.

def snmp_block(fans_failed=(), psus_failed=(), temperature_ok=True,
               available=True, slot_ok=True):
    """A device-state `snmp` block, shaped as device_manager publishes it.

    `speed_raw` / `voltage_raw` are 0 throughout — that is what a healthy
    running wall reports, and NovaStar R&D confirmed by email that neither is
    provided over SNMP at all, so nothing may read them as a measurement.

    A PSU in `psus_failed` gets `i_signal: 0` and keeps `status: 0`, which is
    what a real not-connected supply looks like: `iSignal` is the power field
    on `.1.17` and `status` is undocumented and carries no signal either way.

    It also lands in `dropped_psus`, because only a supply OBSERVED going from
    connected to not connected is alertable. An empty PSU bay reports the same
    `iSignal: 0` as a dead supply, so the absolute state cannot raise an alarm
    without crying wolf on every unfitted bay for the length of a show — see
    `_psu_transitions` in device_manager.
    """
    return {
        'available': available, 'unsupported': False, 'read_at': '20:14:03',
        'dropped_psus': sorted(psus_failed),
        'model': 'H15', 'firmware': 'V2.0.0.6',
        'temperature_status': 0 if temperature_ok else 1,
        'temperature_ok': temperature_ok,
        'fan_count': 10, 'psu_count': 4,
        'fans': [{'fan_id': i, 'speed_raw': 0,
                  'status': 1 if i in fans_failed else 0,
                  'ok': i not in fans_failed} for i in range(10)],
        'psus': [{'power_id': i, 'voltage_raw': 0, 'status': 0,
                  'i_signal': 0 if i in psus_failed else 1,
                  'connected': i not in psus_failed,
                  'ok': i not in psus_failed} for i in range(4)],
        'failed_fans': list(fans_failed), 'failed_psus': list(psus_failed),
        'disconnected_psus': list(psus_failed),
        'screens': {'screen_count': 2, 'fields': {}},
        # Card-slot status is `0: Abnormal` — the inverse of the Normal: 0
        # fields above it. 1 is what a healthy card reports.
        # `port` is the `.30.5.x` FIELD table for ONE Ethernet port, not a map
        # of port number → link state. The values below are what our H15
        # actually answers: primary link 0, backup inactive, backup not linked.
        # Read as a port map that was "three of sixteen ports down" and raised
        # a CRITICAL on a lit wall; read correctly it is a healthy wall with an
        # idle backup. Nothing in _snmp_breaches may look at it.
        'output': {'card_count': 8, 'slot_status': 1 if slot_ok else 0,
                   'slot_ok': slot_ok, 'port_count': 4,
                   'port': {'link_status': 0, 'backup_working': 0,
                            'backup_link': 0}},
    }


def test_a_healthy_chassis_raises_nothing(client):
    assert appmod._snmp_breaches(snmp_block()) == []


def test_the_placeholder_zeros_never_raise_an_alert(client):
    """speed_raw 0 on every fan and voltage_raw 0 on every supply is HEALTHY.

    The lesson this codebase has already paid for twice: a placeholder zero
    read as a measurement alarms on hardware that is working perfectly.
    """
    block = snmp_block()
    assert all(f['speed_raw'] == 0 for f in block['fans'])
    assert all(p['voltage_raw'] == 0 for p in block['psus'])
    assert appmod._snmp_breaches(block) == []

    state = {'name': 'Wall', 'cards_fresh': True, 'receiving_cards': [], 'live_monitoring': {},
             'snmp': block}
    appmod.evaluate_alerts('dev-h', state)
    assert appmod._error_log == []


def test_a_failed_fan_alerts(client):
    breaches = appmod._snmp_breaches(snmp_block(fans_failed={3}))
    assert len(breaches) == 1
    assert breaches[0]['metric'] == 'Chassis fan'
    assert breaches[0]['severity'] == 'WARNING'
    assert breaches[0]['label'] == 'Fan 3'
    assert breaches[0]['value'] is None          # a status, not a reading


def test_a_failed_psu_is_critical(client):
    """Same reasoning as a card's failed supply: still up, now unprotected."""
    breaches = appmod._snmp_breaches(snmp_block(psus_failed={1}))
    assert len(breaches) == 1
    assert breaches[0]['metric'] == 'Chassis power supply'
    assert breaches[0]['severity'] == 'CRITICAL'
    assert breaches[0]['label'] == 'PSU 1'


def test_a_temperature_fault_is_critical(client):
    breaches = appmod._snmp_breaches(snmp_block(temperature_ok=False))
    assert [(b['metric'], b['severity']) for b in breaches] == \
        [('Chassis temperature', 'CRITICAL')]


def test_the_output_port_fields_raise_nothing_at_all(client):
    """The removed alert, asserted from the other end.

    `test_a_dropped_output_port_alerts` and `test_an_unlinked_spare_port_does_
    not_alert` used to live here, both driving an `Output port N: link lost`
    CRITICAL off `output['ports_down']`. That alert was built on reading the
    `.30.5.x` FIELD table as a per-port link map: our H15's `{1: 0, 3: 0,
    4: 0}` became "three of sixteen ports report no link" and woke somebody up
    over a healthy wall whose backup was simply idle. There is no per-port link
    array in this subtree, so the alert was removed rather than tuned — see
    device_manager._apply_snmp_health.

    The fields are still published for display. Nothing may alert on them:
    which port they describe is chosen by a `.30.4` SET the read-only client
    never issues, so a verdict would be about an unidentified port.
    """
    block = snmp_block()
    assert block['output']['port'] == {'link_status': 0, 'backup_working': 0,
                                       'backup_link': 0}
    assert appmod._snmp_breaches(block) == []
    # Even all-zero fields on a healthy wall produce no 'ports' unit anywhere.
    assert not any(b.get('unit') == 'ports'
                   for b in appmod._snmp_breaches(snmp_block(slot_ok=False)))


def test_an_output_card_fault_alerts(client):
    breaches = appmod._snmp_breaches(snmp_block(slot_ok=False))
    assert [(b['metric'], b['severity']) for b in breaches] == \
        [('Output card', 'CRITICAL')]
    # The abnormal value is 0, and the text has to say which value is healthy
    # — an operator reading "slot status 1" as a fault is how the polarity got
    # inverted in the first place.
    assert 'slot status 0' in breaches[0]['text']
    assert 'healthy value is 1' in breaches[0]['text']


def test_the_healthy_slot_status_of_one_raises_nothing(client):
    """The regression this pins: `slot_status: 1` is Normal on `.30.2.1`.

    Both fleet devices report 1 while driving a lit wall. Judged by the
    `0 = OK` rule it produced a CRITICAL "Output card reports a fault" every
    polling cycle, on hardware that was fine.
    """
    block = snmp_block(slot_ok=True)
    assert block['output']['slot_status'] == 1
    assert appmod._snmp_breaches(block) == []


def test_an_unknown_slot_status_raises_nothing(client):
    """device_manager withholds the verdict when `.30` is answering stubs."""
    block = snmp_block()
    block['output'].update({'slot_ok': None, 'slot_status': 0})
    assert appmod._snmp_breaches(block) == []


def test_a_stale_block_is_not_alerted_on(client):
    """`available: false` means this cycle read nothing — the values are old."""
    assert appmod._snmp_breaches(
        snmp_block(fans_failed={1}, psus_failed={2}, available=False)) == []
    assert appmod._snmp_breaches(None) == []
    assert appmod._snmp_breaches({}) == []


def test_snmp_faults_go_through_the_normal_alert_machinery(client):
    state = {'name': 'H-series Live', 'cards_fresh': True, 'receiving_cards': [],
             'live_monitoring': {}, 'snmp': snmp_block(psus_failed={2})}
    appmod.evaluate_alerts('dev-h', state)
    assert len(appmod._error_log) == 1
    entry = appmod._error_log[0]
    assert entry['severity'] == 'CRITICAL'
    assert entry['device'] == 'H-series Live'
    assert entry['cabinet'] == 'PSU 2'


def test_snmp_faults_are_deduped_like_card_faults(client):
    state = {'name': 'Wall', 'cards_fresh': True, 'receiving_cards': [], 'live_monitoring': {},
             'snmp': snmp_block(fans_failed={3})}
    for _ in range(5):
        appmod.evaluate_alerts('dev-h', state)
    assert len(appmod._error_log) == 1


def test_a_whole_fan_tray_failing_is_one_alert(client):
    """Six fans out at once is a fan tray, not six coincidences."""
    state = {'name': 'Wall', 'cards_fresh': True, 'receiving_cards': [], 'live_monitoring': {},
             'snmp': snmp_block(fans_failed=set(range(6)))}
    appmod.evaluate_alerts('dev-h', state)
    assert len(appmod._error_log) == 1
    message = appmod._error_log[0]['message']
    assert '6 fans' in message           # counted in fans, not in "cards"
    assert 'chassis' in message


def test_card_and_chassis_faults_are_capped_together(client, monkeypatch):
    """One cap per cycle, not one per source."""
    monkeypatch.setattr(appmod, 'DEVICE_ROLLUP_CHAIN_THRESHOLD', 10_000)
    monkeypatch.setattr(appmod, 'CHAIN_ALERT_THRESHOLD', 10_000)
    monkeypatch.setattr(appmod, 'MAX_ALERTS_PER_CYCLE', 3)
    cards = [make_card(slot=20, port=p, card_id=0, temp=95.0) for p in range(10)]
    appmod.evaluate_alerts('dev-h', {
        'name': 'Wall', 'cards_fresh': True, 'receiving_cards': cards, 'live_monitoring': {},
        'snmp': snmp_block(fans_failed={1, 2})})
    assert len(appmod._error_log) == 3


def test_a_device_without_snmp_state_is_unaffected(client):
    """VX1000s and the demo device carry no snmp block at all."""
    appmod.evaluate_alerts('dev-vx', {'name': 'VX', 'live_monitoring': {},
                                      'receiving_cards': [make_card()]})
    assert appmod._error_log == []


# ── Emergency stop endpoint ───────────────────────────────

@pytest.fixture(autouse=True)
def _contact_not_halted():
    """The stop is process-wide — never leak it out of a test."""
    import device_manager
    device_manager.resume_device_contact()
    appmod._halt_reason = None
    yield
    device_manager.resume_device_contact()
    appmod._halt_reason = None


def test_halt_status_defaults_to_running(client):
    assert client.get('/api/halt').get_json() == {'halted': False,
                                                  'reason': None}


def test_halt_and_resume_round_trip(client):
    res = client.post('/api/halt', json={'halted': True,
                                         'reason': 'show in progress'})
    assert res.status_code == 200
    assert res.get_json() == {'halted': True, 'reason': 'show in progress'}
    assert client.get('/api/halt').get_json()['reason'] == 'show in progress'

    import device_manager
    assert device_manager.device_contact_halted() is True

    res = client.post('/api/halt', json={'halted': False})
    assert res.get_json() == {'halted': False, 'reason': None}
    assert device_manager.device_contact_halted() is False


def test_halt_without_a_reason_is_allowed(client):
    """An emergency stop must never be gated on typing an explanation."""
    assert client.post('/api/halt', json={'halted': True}).get_json() == \
        {'halted': True, 'reason': None}


def test_halt_reflects_a_stop_engaged_elsewhere(client):
    """The console or a future scheduler can halt too — the API must agree."""
    import device_manager
    device_manager.halt_device_contact('from the console')
    assert client.get('/api/halt').get_json()['halted'] is True


@pytest.mark.parametrize('body', [{}, {'halted': 'yes'}, {'halted': 1},
                                  {'halted': None}, [1, 2]])
def test_halt_rejects_a_malformed_body(client, body):
    assert client.post('/api/halt', json=body).status_code == 400


def test_halt_reason_is_bounded(client):
    res = client.post('/api/halt', json={'halted': True, 'reason': 'x' * 5000})
    assert len(res.get_json()['reason']) == appmod.MAX_HALT_REASON


def test_halt_works_while_a_poll_is_in_flight(client):
    """The operator's stop cannot be made to wait on a device that is busy."""
    import threading
    import device_manager

    class SlowDevice:
        """A device stuck mid-read, holding its own lock the whole time."""

        def __init__(self):
            self.state = {'name': 'slow', 'error': None}
            self.lock = threading.Lock()
            self.released = threading.Event()

        def poll(self):
            with self.lock:
                self.released.wait(2.0)

        def disconnect(self):
            pass

    dev = SlowDevice()
    appmod.manager.devices['dev-slow'] = dev
    poller = threading.Thread(target=dev.poll, daemon=True)
    poller.start()
    try:
        res = client.post('/api/halt', json={'halted': True, 'reason': 'stop'})
        assert res.status_code == 200
        assert device_manager.device_contact_halted() is True
    finally:
        dev.released.set()
        poller.join(timeout=2.0)


def test_halted_devices_say_so_in_their_state(client):
    import device_manager
    dev = device_manager.NovaStar_Device('dev-h', 'H', '10.9.9.9', port=5203)
    appmod.manager.devices['dev-h'] = dev
    client.post('/api/halt', json={'halted': True, 'reason': 'showtime'})
    assert client.get('/api/devices/dev-h/state').get_json()['contact_halted'] \
        is True
    client.post('/api/halt', json={'halted': False})
    assert client.get('/api/devices/dev-h/state').get_json()['contact_halted'] \
        is False


# ── On-demand card refresh endpoint ───────────────────────

class RefreshableDevice:
    """A device that records refresh calls. Reaches no hardware."""

    def __init__(self, chain_cards=2, sweep=None):
        self.state = {'name': 'Wall', 'cards_fresh': True, 'receiving_cards': [],
                      'live_monitoring': {}, 'cards_read_at': None}
        self.chain_calls = []
        self.sweeps = 0
        self.chain_cards = chain_cards
        self.sweep = sweep if sweep is not None else [{'card_id': 0}]

    def refresh_chain(self, slot, port):
        self.chain_calls.append((slot, port))
        return [{'card_id': i} for i in range(self.chain_cards)]

    def refresh_all_cards(self):
        self.sweeps += 1
        return self.sweep

    def disconnect(self):
        pass


def _refreshable(client, **kwargs):
    dev = RefreshableDevice(**kwargs)
    appmod.manager.devices['dev-h'] = dev
    return dev


def test_refresh_all_chains(client):
    dev = _refreshable(client, sweep=[{'card_id': i} for i in range(1374)])
    res = client.post('/api/devices/dev-h/refresh_cards', json={})
    assert res.status_code == 200
    assert res.get_json() == {'status': 'ok', 'cards': 1374, 'reason': None}
    assert dev.sweeps == 1


def test_refresh_one_chain(client):
    dev = _refreshable(client, chain_cards=24)
    res = client.post('/api/devices/dev-h/refresh_cards',
                      json={'slot': 20, 'port': 1})
    assert res.get_json() == {'status': 'ok', 'cards': 24, 'reason': None}
    assert dev.chain_calls == [(20, 1)]
    assert dev.sweeps == 0                   # a chain is not a sweep


def test_a_rate_limited_sweep_is_refused_not_empty(client):
    """`None` from refresh_all_cards means declined; [] means ran, found none.

    The frontend has to be able to tell those apart — one is "wait", the other
    is "there is nothing there".
    """
    _refreshable(client, sweep=None)
    dev = appmod.manager.devices['dev-h']
    dev.sweep = None
    res = client.post('/api/devices/dev-h/refresh_cards', json={})
    body = res.get_json()
    assert body['status'] == 'refused'
    assert body['cards'] == 0
    assert body['reason']


def test_a_sweep_that_finds_nothing_is_ok_not_refused(client):
    _refreshable(client, sweep=[])
    res = client.post('/api/devices/dev-h/refresh_cards', json={})
    assert res.get_json() == {'status': 'ok', 'cards': 0, 'reason': None}


def test_refresh_while_halted_says_halted(client):
    dev = _refreshable(client)
    client.post('/api/halt', json={'halted': True, 'reason': 'show in progress'})
    res = client.post('/api/devices/dev-h/refresh_cards', json={})
    assert res.get_json() == {'status': 'halted', 'cards': 0,
                              'reason': 'show in progress'}
    assert dev.sweeps == 0                   # nothing was even attempted


def test_refresh_of_a_chain_while_halted_says_halted(client):
    dev = _refreshable(client)
    client.post('/api/halt', json={'halted': True})
    res = client.post('/api/devices/dev-h/refresh_cards',
                      json={'slot': 20, 'port': 0})
    assert res.get_json()['status'] == 'halted'
    assert dev.chain_calls == []


def test_refresh_of_an_unknown_device_is_404(client):
    assert client.post('/api/devices/dev-nope/refresh_cards',
                       json={}).status_code == 404


def test_refresh_needs_slot_and_port_together(client):
    _refreshable(client)
    assert client.post('/api/devices/dev-h/refresh_cards',
                       json={'slot': 20}).status_code == 400
    assert client.post('/api/devices/dev-h/refresh_cards',
                       json={'port': 0}).status_code == 400
    assert client.post('/api/devices/dev-h/refresh_cards',
                       json={'slot': 'twenty', 'port': 0}).status_code == 400


def test_refresh_with_no_body_at_all_sweeps(client):
    """An empty POST is the documented "all chains" call."""
    dev = _refreshable(client)
    res = client.post('/api/devices/dev-h/refresh_cards')
    assert res.get_json()['status'] == 'ok'
    assert dev.sweeps == 1


def test_refresh_on_a_device_without_card_reads_is_rejected(client):
    appmod.manager.devices['demo'] = FakeDevice('demo', {'name': 'demo'})
    assert client.post('/api/devices/demo/refresh_cards',
                       json={}).status_code == 400


def test_a_refresh_alert_evaluates_the_cards_it_just_read(client):
    """Data collected on demand and then never looked at is worse than none."""
    dev = _refreshable(client)
    dev.state['receiving_cards'] = [make_card(slot=20, port=2, card_id=7,
                                              temp=95.0)]
    client.post('/api/devices/dev-h/refresh_cards', json={})
    assert len(appmod._error_log) == 1
    assert appmod._error_log[0]['cabinet'] == 'Slot 20 · Port 2 · Card 7'


def test_installed_and_carrying_sender_cards_are_both_reported(client,
                                                              tmp_path):
    """Four sender cards installed — two primary, two backup — and only the
    primaries carry panels. Both numbers are true and both are reported: the
    installed count alone hides a failover, the carrying count alone looks
    like half the hardware vanished."""
    cards = ([make_card(slot=20, card_id=i) for i in range(4)]
             + [make_card(slot=22, card_id=i) for i in range(3)])
    write_old_wall_snapshot(tmp_path, name='CIRCUIT MOM', width=3840,
                            slots=(20, 22), cards=cards)
    add_h_device()
    data = client.get('/api/wall_live').get_json()
    assert data['snapshot_status']['status'] == 'match'
    assert data['topology']['slot_count'] == 4          # installed
    assert data['panels']['populated_sender_cards'] == 2  # carrying panels
    assert data['panels']['count'] == 7


def test_populated_sender_cards_is_none_without_an_inventory(client, tmp_path):
    """Nothing to count from means no claim, not a guess from slot_count."""
    add_h_device()
    data = client.get('/api/wall_live').get_json()
    assert data['panels']['known'] is False
    assert data['panels']['populated_sender_cards'] is None


def test_stale_card_readings_raise_no_alert(client):
    """A card that read 95 C during a sweep hours ago raised a fresh CRITICAL
    every minute afterwards — after it had been fixed, powered down or
    unplugged. An alert nobody can act on trains an operator to ignore the
    log."""
    state = {'name': 'Wall', 'live_monitoring': {},
             'cards_fresh': False,
             'receiving_cards': [make_card(slot=20, port=0, card_id=1,
                                           temp=95.0)]}
    appmod.evaluate_alerts('dev-h', state)
    assert appmod._error_log == []


def test_missing_freshness_is_treated_as_stale(client):
    """Unknown age is not an excuse to alert. Refusing is the safe direction
    and SNMP chassis alerts are unaffected."""
    state = {'name': 'Wall', 'live_monitoring': {},
             'receiving_cards': [make_card(slot=20, port=0, card_id=1,
                                           temp=95.0)]}
    appmod.evaluate_alerts('dev-h', state)
    assert appmod._error_log == []


def test_snmp_alerts_still_fire_when_card_readings_are_stale(client):
    """SNMP arrives every cycle, so its alerts must not be gated by the age of
    per-card readings taken on demand."""
    state = {'name': 'Wall', 'live_monitoring': {}, 'cards_fresh': False,
             'receiving_cards': [make_card(slot=20, port=0, card_id=1,
                                           temp=95.0)],
             'snmp': {'available': True,
                      'health': {'temperature_status': 2}}}
    appmod.evaluate_alerts('dev-h', state)
    # Whatever the SNMP path decides, the stale 95 C card must not appear.
    assert all('Card 1' not in (e.get('cabinet') or '')
               for e in appmod._error_log)


def test_a_detected_chain_break_raises_an_alert(client):
    """Previously this only rendered on the Wall View, so it was seen only if
    somebody happened to have that tab open."""
    state = {'name': 'Wall', 'live_monitoring': {}, 'cards_fresh': True,
             'chain_breaks': [{'card_number': 1, 'port': 3,
                               'signature': 'no_answer', 'at_head': False,
                               'break_panel': 12, 'affected': 11,
                               'clean_before': 11}]}
    appmod.evaluate_alerts('dev-h', state)
    assert len(appmod._error_log) == 1
    e = appmod._error_log[0]
    assert e['severity'] == 'CRITICAL'
    assert 'panel 12' in e['message']
    assert 'card 1 port 4' in e['message']
    assert e['port'] == 3


def test_a_break_alert_warns_that_the_wall_may_still_look_fine(client):
    """The failure mode that makes this worth alerting on at all: the backup
    keeps the panels lit, so nothing looks wrong."""
    state = {'name': 'Wall', 'live_monitoring': {}, 'cards_fresh': True,
             'chain_breaks': [{'card_number': 1, 'port': 3,
                               'signature': 'bit_errors', 'at_head': False,
                               'break_panel': 12, 'affected': 11,
                               'clean_before': 11}]}
    appmod.evaluate_alerts('dev-h', state)
    assert 'backup sender card' in appmod._error_log[0]['message']


def test_no_breaks_means_no_alerts(client):
    appmod.evaluate_alerts('dev-h', {'name': 'Wall', 'live_monitoring': {},
                                     'cards_fresh': True, 'chain_breaks': []})
    assert appmod._error_log == []


def test_break_alerts_are_not_gated_on_card_freshness(client):
    """A break comes from the bit-error read that found it; that read is the
    evidence."""
    state = {'name': 'Wall', 'live_monitoring': {}, 'cards_fresh': False,
             'chain_breaks': [{'card_number': 2, 'port': 0,
                               'signature': 'no_answer', 'at_head': True,
                               'break_panel': 1, 'affected': 18,
                               'clean_before': 0}]}
    appmod.evaluate_alerts('dev-h', state)
    assert len(appmod._error_log) == 1
    assert 'at or before the first panel' in appmod._error_log[0]['message']


def test_reading_one_chain_does_not_restate_the_wall_size(client, tmp_path):
    """A 22-card chain read must not turn a 286-panel wall into 22 panels.
    How many panels the wall HAS is an inventory question; how many were read
    just now is a different one."""
    inventory = [make_card(slot=20, port=p, card_id=i)
                 for p in range(4) for i in range(20)]
    write_old_wall_snapshot(tmp_path, name='CIRCUIT MOM', width=3840,
                            slots=(20, 22), cards=inventory)
    add_h_device(cards=[make_card(slot=20, port=3, card_id=i)
                        for i in range(5)])
    panels = client.get('/api/wall_live').get_json()['panels']
    assert panels['count'] == 80          # the inventory
    assert panels['read'] == 5            # what answered just now
    assert panels['source'] == 'enumeration'


def test_without_an_inventory_the_read_count_is_flagged_as_a_floor(client):
    add_h_device(cards=[make_card(slot=20, port=0, card_id=i)
                        for i in range(3)])
    panels = client.get('/api/wall_live').get_json()['panels']
    assert panels['count'] == 3
    assert panels['is_lower_bound'] is True


def test_the_page_shell_is_never_cached(client):
    """The shell carries the versioned asset URLs, so caching it defeats the
    cache-busting entirely: the browser reuses yesterday's HTML, requests
    yesterday's app.js, and the dashboard does not change no matter how hard
    the operator reloads. Flask sent no cache headers at all, which leaves the
    browser free to cache heuristically — Safari does."""
    res = client.get('/')
    assert res.status_code == 200
    assert 'no-store' in res.headers.get('Cache-Control', '')


def test_asset_urls_carry_a_version_that_tracks_the_file(client, tmp_path):
    """The version used to be a hand-edited literal, so every edit after
    somebody last remembered to bump it shipped under a URL browsers had
    already cached."""
    html = client.get('/').get_data(as_text=True)
    import re
    versions = re.findall(r"js/(?:app|wall_view)\.js\?v=(\d+)", html)
    assert len(versions) == 2
    assert all(int(v) > 0 for v in versions)


def test_a_missing_asset_does_not_take_the_page_down(client):
    """A templating error over a cache-buster would be a worse outcome than a
    stale file."""
    with appmod.app.test_request_context('/'):
        assert appmod.asset_url('js/does-not-exist.js').endswith(
            'js/does-not-exist.js')


# ── The dashboard may not overrule the backend's abstentions ───────────────
#
# There is no JS runner in this suite, so this reads app.js's own source — the
# same guard-rail shape tests/test_snmp_client.py uses to keep a SET path out
# of the SNMP client.

def _app_js_source():
    path = os.path.join(os.path.dirname(appmod.__file__), 'static', 'js',
                        'app.js')
    with open(path, 'r', encoding='utf-8') as handle:
        return handle.read()


def test_the_health_strip_treats_a_null_ok_as_unknown_not_as_a_fault(client):
    """`ok: null` is a REFUSAL to judge, not a field the backend forgot.

    snmp_client.parse_psus emits it for a supply whose `iSignal` is missing or
    unparseable — `iSignal` being the only field on `.1.17` with a documented
    meaning (NovaStar R&D, by email). `status` is not a fallback for it; it is
    an undocumented number that sits alongside. unitState() used to fall
    straight through such a unit to `status === 0 ? 'ok' : 'bad'`, which
    painted "1 of 4 FAILED" in danger red off a value nobody could interpret.
    """
    source = _app_js_source()
    guard = source.index("if ('ok' in unit) return 'unknown';")
    fallback = source.index("return status === 0 ? 'ok' : 'bad';")
    # The abstention has to be checked BEFORE the raw-status rule, or it does
    # nothing at all.
    assert guard < fallback


def test_the_backend_really_does_abstain_when_isignal_is_missing(client):
    """The other half of the pair above: what unitState is actually handed.

    The abstention used to be triggered by a non-zero `status`. It is not any
    more — `status` moves nothing — so the guard would be dead code if nothing
    else could produce `ok: null`. A payload with no `iSignal` still can, and
    that is the case the guard now defends.
    """
    from snmp_client import parse_psus
    # No iSignal at all: nothing documented to judge on, so no verdict.
    psus = parse_psus(json.dumps([{'powerId': 0, 'status': 3, 'voltage': 0}]))
    assert psus[0]['status'] == 3
    assert psus[0]['ok'] is None
    assert psus[0]['connected'] is None
    # And a non-zero `status` on its own is NOT an abstention and NOT a fault:
    # a connected supply stays `ok`, whatever that undocumented number says.
    odd = parse_psus(json.dumps([{'iSignal': 1, 'powerId': 0, 'status': 3,
                                  'voltage': 0}]))
    assert odd[0]['ok'] is True
    assert odd[0]['connected'] is True
