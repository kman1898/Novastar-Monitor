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
        'receiving_cards': cards, 'screen_outputs': {}, 'device_info': {},
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
        'last_poll': '12:00:01', 'receiving_cards': [], 'device_info': {},
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
             'receiving_cards': [make_card(slot=20, port=2, card_id=7, temp=95.0)],
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
    breaches = appmod._card_breaches([make_card(voltage=4.2)], DEFAULTS)
    assert breaches[0]['severity'] == 'WARNING'


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
             'receiving_cards': [power_card(slot=20, port=2, card_id=7,
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
             'receiving_cards': [power_card(slot=20, port=0, card_id=1,
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
    appmod.evaluate_alerts('dev-h', {'name': 'Wall', 'receiving_cards': cards,
                                     'live_monitoring': {}})
    assert len(appmod._error_log) == 1
    assert '40 cards' in appmod._error_log[0]['message']
    assert appmod._error_log[0]['port'] == 3


def test_wall_wide_power_failure_collapses_to_one_alert(client):
    cards = [power_card(slot=20, port=p, card_id=i, primary=False)
             for p in range(6) for i in range(20)]
    appmod.evaluate_alerts('dev-h', {'name': 'Wall', 'receiving_cards': cards,
                                     'live_monitoring': {}})
    assert len(appmod._error_log) == 1
    assert '6 chains' in appmod._error_log[0]['message']


def test_power_and_temperature_faults_are_separate_alerts(client):
    """One card failing two ways is two events — they group independently."""
    card = power_card(slot=20, port=1, card_id=4, temp=95.0, primary=False)
    metrics = {b['metric'] for b in appmod._card_breaches([card], DEFAULTS)}
    assert metrics == {'Temperature', 'Power supply'}

    appmod.evaluate_alerts('dev-h', {'name': 'Wall', 'receiving_cards': [card],
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
             'receiving_cards': [bit_card(0xFFFF, saturated=True, slot=20,
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


def test_alerts_are_deduped_per_card(client):
    state = {'name': 'Wall',
             'receiving_cards': [make_card(slot=20, port=0, card_id=1, temp=95.0)],
             'live_monitoring': {}}
    for _ in range(5):
        appmod.evaluate_alerts('dev-h', state)
    assert len(appmod._error_log) == 1


def test_dedupe_is_per_card_not_global(client):
    """Two different hot cards are two incidents, not one deduped incident."""
    state = {'name': 'Wall', 'live_monitoring': {}, 'receiving_cards': [
        make_card(slot=20, port=0, card_id=1, temp=95.0),
        make_card(slot=20, port=0, card_id=2, temp=96.0),
    ]}
    appmod.evaluate_alerts('dev-h', state)
    assert len(appmod._error_log) == 2


def test_dedupe_survives_a_burst_larger_than_the_log_tail(client):
    """The old dedupe scanned only the last 20 entries — a burst defeated it."""
    cards = [make_card(slot=20, port=p, card_id=0, temp=95.0) for p in range(2)]
    for _ in range(60):
        appmod.evaluate_alerts('dev-h', {'name': 'Wall', 'receiving_cards': cards,
                                         'live_monitoring': {}})
    assert len(appmod._error_log) == 2


def test_chain_wide_failure_is_summarised_as_one_alert(client):
    """40 hot cards on one chain is one event, not 40."""
    cards = [make_card(slot=20, port=3, card_id=i, temp=95.0) for i in range(40)]
    appmod.evaluate_alerts('dev-h', {'name': 'Wall', 'receiving_cards': cards,
                                     'live_monitoring': {}})
    assert len(appmod._error_log) == 1
    entry = appmod._error_log[0]
    assert '40 cards' in entry['message']
    assert entry['port'] == 3


def test_wall_wide_failure_collapses_to_one_alert(client):
    cards = [make_card(slot=20, port=p, card_id=i, temp=95.0)
             for p in range(6) for i in range(20)]
    appmod.evaluate_alerts('dev-h', {'name': 'Wall', 'receiving_cards': cards,
                                     'live_monitoring': {}})
    assert len(appmod._error_log) == 1
    assert '6 chains' in appmod._error_log[0]['message']


def test_alert_volume_is_capped_per_cycle(client, monkeypatch):
    monkeypatch.setattr(appmod, 'DEVICE_ROLLUP_CHAIN_THRESHOLD', 10_000)
    monkeypatch.setattr(appmod, 'CHAIN_ALERT_THRESHOLD', 10_000)
    monkeypatch.setattr(appmod, 'MAX_ALERTS_PER_CYCLE', 5)
    cards = [make_card(slot=20, port=p, card_id=0, temp=95.0) for p in range(50)]
    appmod.evaluate_alerts('dev-h', {'name': 'Wall', 'receiving_cards': cards,
                                     'live_monitoring': {}})
    assert len(appmod._error_log) == 5


def test_vx1000_style_cards_are_identified_by_label(client):
    cards = [{'index': 4, 'label': 'P3C05', 'port': 3, 'online': True,
              'temperature_c': 95.0, 'voltage_v': 5.0}]
    appmod.evaluate_alerts('dev-vx', {'name': 'VX', 'receiving_cards': cards,
                                      'live_monitoring': {}})
    assert appmod._error_log[0]['cabinet'] == 'P3C05'
    assert appmod._error_log[0]['port'] == 3


def test_device_rollup_uses_max_not_mean(client):
    """The rollup reads temperature_max_c; the mean can never cross a threshold."""
    state = {'name': 'Wall', 'receiving_cards': [], 'live_monitoring': {
        'card_count': 1374, 'temperature_c': 44.1, 'temperature_max_c': 95.0,
    }}
    appmod.evaluate_alerts('dev-h', state)
    assert len(appmod._error_log) == 1
    assert 'Hottest card 95.0' in appmod._error_log[0]['message']
    assert appmod._error_log[0]['severity'] == 'CRITICAL'


def test_device_rollup_quiet_when_max_is_healthy(client):
    state = {'name': 'Wall', 'receiving_cards': [], 'live_monitoring': {
        'card_count': 1374, 'temperature_c': 44.1, 'temperature_max_c': 50.0,
    }}
    appmod.evaluate_alerts('dev-h', state)
    assert appmod._error_log == []


def test_device_rollup_uses_voltage_min_when_available(client):
    state = {'name': 'Wall', 'receiving_cards': [], 'live_monitoring': {
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
