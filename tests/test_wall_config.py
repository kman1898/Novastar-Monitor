"""Tests for wall_config.py — topology loader, layout merger, validators."""
import json
import os
import tempfile

import pytest
import wall_config as wc


# ── Path resolution & loading ─────────────────────────────────────────────

SRC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


@pytest.fixture
def temp_app_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


class TestLoadDefaults:
    """Default configs ship with the app and load when no user override exists."""

    def test_load_config_default(self, temp_app_dir):
        config = wc.load_config(temp_app_dir, SRC_DIR)
        assert config["wall_name"] == "Main Wall"
        assert len(config["pillars"]) == 7
        assert len(config["ports"]) == 15

    def test_load_layout_default(self, temp_app_dir):
        layout = wc.load_layout(temp_app_dir, SRC_DIR)
        assert layout["layout_name"] == "default"
        assert layout["pillar_overrides"] == {}

    def test_user_config_wins_over_default(self, temp_app_dir):
        custom = {
            "wall_name": "Test Wall",
            "panel": {"width": 60, "height": 120},
            "pillars": [],
            "ports": [],
        }
        with open(os.path.join(temp_app_dir, "wall_config.json"), "w") as f:
            json.dump(custom, f)
        config = wc.load_config(temp_app_dir, SRC_DIR)
        assert config["wall_name"] == "Test Wall"

    def test_user_layout_wins_over_default(self, temp_app_dir):
        custom = {"layout_name": "custom", "pillar_overrides": {"x": {"position": {"x": 1, "y": 2}}}}
        with open(os.path.join(temp_app_dir, "wall_layout.json"), "w") as f:
            json.dump(custom, f)
        layout = wc.load_layout(temp_app_dir, SRC_DIR)
        assert layout["layout_name"] == "custom"


# ── Default config integrity ──────────────────────────────────────────────


class TestDefaultConfigContent:
    """The bundled default describes Matt's actual wall — verify chain counts."""

    @pytest.fixture
    def config(self, temp_app_dir):
        return wc.load_config(temp_app_dir, SRC_DIR)

    def test_pillar_ids(self, config):
        ids = {p["id"] for p in config["pillars"]}
        assert ids == {"srr", "sr", "main_top", "sl", "sll", "dj_booth", "main_bottom"}

    def test_port_count(self, config):
        port_nums = sorted(p["port"] for p in config["ports"])
        assert port_nums == list(range(1, 16))

    def test_total_card_count(self, config):
        # 76+50+84+84+42+87+91+52+84+84+42+76+50+15+26 = 943
        assert wc.total_card_count(config) == 943

    def test_a7_is_max(self, config):
        # A7 (port 7, Main Top) should be the largest chain at 91 cards
        a7 = wc.get_port(config, 7)
        assert a7["card_count"] == 91
        assert a7["pillar"] == "main_top"

    def test_main_bottom_at_canvas_position(self, config):
        # Per user spec: Main Bottom canvas coord is (2220, 1920)
        mb = wc.get_pillar(config, "main_bottom")
        assert mb["canvas_position"] == {"x": 2220, "y": 1920}

    def test_dj_booth_single_chain(self, config):
        a14 = wc.get_port(config, 14)
        assert a14["pillar"] == "dj_booth"
        assert a14["card_count"] == 15

    def test_pillars_have_port_assignments(self, config):
        for pillar in config["pillars"]:
            assert "ports" in pillar
            assert len(pillar["ports"]) >= 1


# ── Layout merging ────────────────────────────────────────────────────────


class TestRenderWall:
    """render_wall() merges config + layout overrides into a single view."""

    def test_no_overrides_falls_back_to_canvas(self, temp_app_dir):
        config = wc.load_config(temp_app_dir, SRC_DIR)
        layout = {"layout_name": "default", "pillar_overrides": {}}
        rendered = wc.render_wall(config, layout)
        for pillar in rendered["pillars"]:
            assert pillar["display_position"] == pillar["canvas_position"]

    def test_override_replaces_position(self, temp_app_dir):
        config = wc.load_config(temp_app_dir, SRC_DIR)
        layout = {
            "layout_name": "moved",
            "pillar_overrides": {
                "main_bottom": {"position": {"x": 1440, "y": 2160}}
            },
        }
        rendered = wc.render_wall(config, layout)
        mb = next(p for p in rendered["pillars"] if p["id"] == "main_bottom")
        assert mb["display_position"] == {"x": 1440, "y": 2160}
        assert mb["canvas_position"] == {"x": 2220, "y": 1920}  # canvas unchanged

    def test_partial_overrides(self, temp_app_dir):
        """Only overridden pillars get new positions; others use canvas."""
        config = wc.load_config(temp_app_dir, SRC_DIR)
        layout = {
            "layout_name": "partial",
            "pillar_overrides": {"srr": {"position": {"x": 100, "y": 200}}},
        }
        rendered = wc.render_wall(config, layout)
        srr = next(p for p in rendered["pillars"] if p["id"] == "srr")
        sr = next(p for p in rendered["pillars"] if p["id"] == "sr")
        assert srr["display_position"] == {"x": 100, "y": 200}
        assert sr["display_position"] == sr["canvas_position"]

    def test_render_preserves_layout_name(self, temp_app_dir):
        config = wc.load_config(temp_app_dir, SRC_DIR)
        layout = {"layout_name": "rehearsal", "pillar_overrides": {}}
        rendered = wc.render_wall(config, layout)
        assert rendered["layout_name"] == "rehearsal"

    def test_render_with_none_layout(self, temp_app_dir):
        """render_wall must handle None layout gracefully."""
        config = wc.load_config(temp_app_dir, SRC_DIR)
        rendered = wc.render_wall(config, None)
        assert rendered["layout_name"] == "default"
        for pillar in rendered["pillars"]:
            assert pillar["display_position"] == pillar["canvas_position"]


# ── Save/load round-trip ──────────────────────────────────────────────────


class TestSaveLayout:
    def test_save_round_trip(self, temp_app_dir):
        layout = {
            "layout_name": "test",
            "pillar_overrides": {"srr": {"position": {"x": 50, "y": 100}}},
        }
        wc.save_layout(temp_app_dir, layout)
        loaded = wc.load_layout(temp_app_dir, SRC_DIR)
        assert loaded == layout

    def test_save_creates_file_in_app_dir(self, temp_app_dir):
        wc.save_layout(temp_app_dir, {"layout_name": "x", "pillar_overrides": {}})
        assert os.path.exists(os.path.join(temp_app_dir, "wall_layout.json"))

    def test_save_is_atomic(self, temp_app_dir, monkeypatch):
        """A save that dies mid-write must leave the previous layout intact.

        Truncate-then-write would leave an unparseable file behind, and
        load_layout() would then raise on every wall request until someone
        deleted it by hand.
        """
        first = {"layout_name": "good", "pillar_overrides": {}}
        wc.save_layout(temp_app_dir, first)

        def boom(*args, **kwargs):
            raise RuntimeError("disk went away mid-write")

        monkeypatch.setattr(wc.json, "dump", boom)
        with pytest.raises(RuntimeError):
            wc.save_layout(temp_app_dir, {"layout_name": "bad",
                                          "pillar_overrides": {}})

        monkeypatch.undo()
        assert wc.load_layout(temp_app_dir, SRC_DIR) == first

    def test_failed_save_leaves_no_temp_files(self, temp_app_dir, monkeypatch):
        monkeypatch.setattr(wc.json, "dump",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        with pytest.raises(RuntimeError):
            wc.save_layout(temp_app_dir, {"pillar_overrides": {}})
        assert os.listdir(temp_app_dir) == []

    def test_save_replaces_rather_than_truncates(self, temp_app_dir):
        """os.replace() means a reader never observes a half-written file."""
        wc.save_layout(temp_app_dir, {"layout_name": "one",
                                      "pillar_overrides": {}})
        wc.save_layout(temp_app_dir, {"layout_name": "two",
                                      "pillar_overrides": {}})
        assert os.listdir(temp_app_dir) == ["wall_layout.json"]
        with open(os.path.join(temp_app_dir, "wall_layout.json")) as f:
            assert json.load(f)["layout_name"] == "two"


# ── Config validation ─────────────────────────────────────────────────────


class TestValidateConfig:
    def test_default_config_is_valid(self, temp_app_dir):
        config = wc.load_config(temp_app_dir, SRC_DIR)
        assert wc.validate_config(config) == []

    def test_duplicate_pillar_ids(self):
        config = {
            "pillars": [{"id": "a", "ports": [1]}, {"id": "a", "ports": [2]}],
            "ports": [
                {"port": 1, "pillar": "a", "card_count": 10},
                {"port": 2, "pillar": "a", "card_count": 10},
            ],
        }
        errors = wc.validate_config(config)
        assert any("Duplicate pillar IDs" in e for e in errors)

    def test_duplicate_port_numbers(self):
        config = {
            "pillars": [{"id": "a", "ports": [1]}],
            "ports": [
                {"port": 1, "pillar": "a", "card_count": 10},
                {"port": 1, "pillar": "a", "card_count": 10},
            ],
        }
        errors = wc.validate_config(config)
        assert any("Duplicate port numbers" in e for e in errors)

    def test_port_out_of_range(self):
        config = {
            "pillars": [{"id": "a", "ports": [99]}],
            "ports": [{"port": 99, "pillar": "a", "card_count": 10}],
        }
        errors = wc.validate_config(config)
        assert any("out of range" in e for e in errors)

    def test_port_references_unknown_pillar(self):
        config = {
            "pillars": [{"id": "a", "ports": []}],
            "ports": [{"port": 1, "pillar": "ghost", "card_count": 10}],
        }
        errors = wc.validate_config(config)
        assert any("unknown pillar" in e for e in errors)

    def test_invalid_card_count(self):
        config = {
            "pillars": [{"id": "a", "ports": [1]}],
            "ports": [{"port": 1, "pillar": "a", "card_count": 0}],
        }
        errors = wc.validate_config(config)
        assert any("invalid card_count" in e for e in errors)

    def test_pillar_port_cross_reference_mismatch(self):
        config = {
            "pillars": [
                {"id": "a", "ports": [1, 2]},  # claims port 2
                {"id": "b", "ports": []},
            ],
            "ports": [
                {"port": 1, "pillar": "a", "card_count": 10},
                {"port": 2, "pillar": "b", "card_count": 10},  # but port 2 → b
            ],
        }
        errors = wc.validate_config(config)
        assert any("but port maps to" in e for e in errors)

    def test_non_dict_returns_error(self):
        assert wc.validate_config("not a dict") == ["Config must be a dict"]


class TestValidateLayout:
    def test_empty_layout_valid(self, temp_app_dir):
        config = wc.load_config(temp_app_dir, SRC_DIR)
        assert wc.validate_layout({"layout_name": "x", "pillar_overrides": {}}, config) == []

    def test_layout_must_be_dict(self):
        assert wc.validate_layout("not a dict") == ["Layout must be a dict"]

    def test_overrides_must_be_dict(self):
        errors = wc.validate_layout({"pillar_overrides": []})
        assert any("must be a dict" in e for e in errors)

    def test_unknown_pillar_in_overrides(self, temp_app_dir):
        config = wc.load_config(temp_app_dir, SRC_DIR)
        layout = {"pillar_overrides": {"ghost": {"position": {"x": 0, "y": 0}}}}
        errors = wc.validate_layout(layout, config)
        assert any("unknown pillar" in e for e in errors)

    def test_invalid_position_format(self, temp_app_dir):
        config = wc.load_config(temp_app_dir, SRC_DIR)
        layout = {"pillar_overrides": {"srr": {"position": "bad"}}}
        errors = wc.validate_layout(layout, config)
        assert any("invalid position" in e for e in errors)

    def test_position_xy_must_be_numeric(self, temp_app_dir):
        config = wc.load_config(temp_app_dir, SRC_DIR)
        layout = {"pillar_overrides": {"srr": {"position": {"x": "a", "y": "b"}}}}
        errors = wc.validate_layout(layout, config)
        assert any("must be numeric" in e for e in errors)


# ── Helper accessors ──────────────────────────────────────────────────────


class TestAccessors:
    def test_get_pillar_found(self, temp_app_dir):
        config = wc.load_config(temp_app_dir, SRC_DIR)
        srr = wc.get_pillar(config, "srr")
        assert srr["name"] == "SRR Pillar"

    def test_get_pillar_missing(self, temp_app_dir):
        config = wc.load_config(temp_app_dir, SRC_DIR)
        assert wc.get_pillar(config, "nonexistent") is None

    def test_get_port_found(self, temp_app_dir):
        config = wc.load_config(temp_app_dir, SRC_DIR)
        a1 = wc.get_port(config, 1)
        assert a1["label"] == "A1"
        assert a1["card_count"] == 76

    def test_get_port_missing(self, temp_app_dir):
        config = wc.load_config(temp_app_dir, SRC_DIR)
        assert wc.get_port(config, 99) is None


# ── OPT (fiber) topology ──────────────────────────────────────────────────


class TestOptTopology:
    """Verify the 4-OPT / 2-output-card structure on the H-series."""

    @pytest.fixture
    def config(self, temp_app_dir):
        return wc.load_config(temp_app_dir, SRC_DIR)

    def test_every_port_has_opt_assignment(self, config):
        for port in config["ports"]:
            assert "opt" in port, f"port {port['port']} missing opt"
            assert "opt_subport" in port, f"port {port['port']} missing opt_subport"

    def test_opt_1_carries_ports_1_through_8(self, config):
        assert wc.ports_for_opt(config, 1) == [1, 2, 3, 4, 5, 6, 7, 8]

    def test_opt_2_carries_ports_9_through_15(self, config):
        assert wc.ports_for_opt(config, 2) == [9, 10, 11, 12, 13, 14, 15]

    def test_opt_subport_numbering_is_1_based_within_each_opt(self, config):
        # OPT 1 sub-ports should be 1..8 (one per port on OPT 1)
        opt1_subports = sorted(p["opt_subport"] for p in config["ports"] if p["opt"] == 1)
        assert opt1_subports == [1, 2, 3, 4, 5, 6, 7, 8]
        # OPT 2 sub-ports should be 1..7
        opt2_subports = sorted(p["opt_subport"] for p in config["ports"] if p["opt"] == 2)
        assert opt2_subports == [1, 2, 3, 4, 5, 6, 7]

    def test_output_cards_present(self, config):
        cards = config.get("output_cards", [])
        assert len(cards) == 2
        roles = {c["role"] for c in cards}
        assert roles == {"primary", "backup"}

    def test_get_output_card_primary(self, config):
        primary = wc.get_output_card(config, "O-5")
        assert primary["role"] == "primary"
        assert primary["slot"] == 5
        assert len(primary["opts"]) == 2

    def test_get_output_card_backup(self, config):
        backup = wc.get_output_card(config, "O-6")
        assert backup["role"] == "backup"
        assert backup["mirrors"] == "O-5"

    def test_get_output_card_missing(self, config):
        assert wc.get_output_card(config, "O-99") is None

    def test_ports_for_unknown_opt_returns_empty(self, config):
        # OPT 3 and OPT 4 belong to the backup card and don't carry primary ports
        assert wc.ports_for_opt(config, 3) == []
        assert wc.ports_for_opt(config, 4) == []
        assert wc.ports_for_opt(config, 99) == []
