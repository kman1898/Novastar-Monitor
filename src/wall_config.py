"""
Wall topology configuration loader — the operator-authored PHYSICAL map.

DO NOT DELETE THIS AS DEAD CODE. Nothing in the frontend calls the
`/api/wall_config`, `/api/wall_layout` or `/api/wall_rendered` endpoints today
— the SVG renderer that used to consume them was removed when the Wall View
was rebuilt on live enumeration data (see the header of static/js/wall_view.js
and docs/H_SERIES_FINDINGS.md). That is a *missing UI*, not a dead concept:

  The controller only ever reports LOGICAL topology — sender card → OPT →
  port → chain of receiving cards, addressed as (slot, port, card_id). It has
  no idea where a panel physically hangs in the room. On this wall the panels
  are spread across seven architectural pillars, so "slot 20 · port 4 ·
  card 37 is at 95 °C" is not actionable on its own and "third row of the SL
  pillar" is. Only a human can supply that mapping, and this module is where
  it lives.

What is still missing before this can drive a UI: a bridge from a pillar's
`ports` list to the live path's (slot, port) pairs — the config's `opt` field
is the hook for it — plus the drag-drop editor that writes wall_layout.json.
Both are additive; the loader/merger/validator below are complete and tested.

Two-file architecture:
  - wall_config.json  : authoritative pillar/port/card_count data, canvas
                        coordinates from the H-series controller. Source of
                        truth — only edited when the physical wall changes.
  - wall_layout.json  : user-editable display-position overrides per pillar
                        (drag-drop UI saves to this file). Empty by default.

Both files load from APP_DIR if present, otherwise fall back to the bundled
defaults shipped in BASE_DIR. This lets us ship a sensible default wall while
allowing per-install customization without modifying source files.
"""

import json
import os
import tempfile


def _paths(app_dir, base_dir):
    return {
        "config": os.path.join(app_dir, "wall_config.json"),
        "config_default": os.path.join(base_dir, "wall_config_default.json"),
        "layout": os.path.join(app_dir, "wall_layout.json"),
        "layout_default": os.path.join(base_dir, "wall_layout_default.json"),
    }


def load_config(app_dir, base_dir):
    """Load wall config — user override if present, else bundled default."""
    p = _paths(app_dir, base_dir)
    target = p["config"] if os.path.exists(p["config"]) else p["config_default"]
    with open(target, "r", encoding="utf-8") as f:
        return json.load(f)


def load_layout(app_dir, base_dir):
    """Load wall layout — user override if present, else bundled default."""
    p = _paths(app_dir, base_dir)
    target = p["layout"] if os.path.exists(p["layout"]) else p["layout_default"]
    with open(target, "r", encoding="utf-8") as f:
        return json.load(f)


def save_layout(app_dir, layout):
    """Save user's layout to APP_DIR atomically. Never touches bundled defaults.

    Deliberately a local copy of app._atomic_write_json rather than an import:
    app imports this module, so importing back would be circular, and this
    module has no other reason to know app exists. Twelve duplicated lines is
    the cheaper trade against a shared utility module (which would also need a
    new hiddenimport entry in novastar_monitor.spec).

    Truncate-then-write loses the whole layout if the process dies mid-write
    or two saves interleave — and a half-written wall_layout.json fails to
    parse, so load_layout() raises and the wall endpoints 500 until someone
    deletes the file by hand. os.replace() is atomic on POSIX and Windows, so
    a reader sees either the old layout or the new one.
    """
    target = os.path.join(app_dir, "wall_layout.json")
    os.makedirs(app_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=app_dir, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(layout, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass  # best-effort cleanup; the original error is re-raised below
        raise


def render_wall(config, layout):
    """Combine config + layout overrides into a single rendered wall.

    Each pillar in the result gains a `display_position` field — falls back
    to `canvas_position` when the layout has no override for that pillar.
    """
    overrides = (layout or {}).get("pillar_overrides", {}) or {}
    rendered_pillars = []
    for pillar in config.get("pillars", []):
        override = overrides.get(pillar["id"], {}) or {}
        rendered_pillars.append({
            **pillar,
            "display_position": override.get("position", pillar["canvas_position"]),
        })
    return {
        **config,
        "pillars": rendered_pillars,
        "layout_name": (layout or {}).get("layout_name", "default"),
    }


def validate_config(config):
    """Return list of validation error strings, or [] if config is valid."""
    errors = []
    if not isinstance(config, dict):
        return ["Config must be a dict"]

    pillars = config.get("pillars", [])
    ports = config.get("ports", [])

    if not isinstance(pillars, list) or not isinstance(ports, list):
        return ["pillars and ports must be lists"]

    pillar_ids = [p.get("id") for p in pillars]
    if len(pillar_ids) != len(set(pillar_ids)):
        errors.append("Duplicate pillar IDs")

    port_nums = [p.get("port") for p in ports]
    if len(port_nums) != len(set(port_nums)):
        errors.append("Duplicate port numbers")
    for n in port_nums:
        if not isinstance(n, int) or not 1 <= n <= 15:
            errors.append(f"Port {n!r} out of range (must be int 1-15)")

    pillar_id_set = set(pillar_ids)
    for port in ports:
        pid = port.get("pillar")
        if pid not in pillar_id_set:
            errors.append(
                f"Port {port.get('port')} references unknown pillar {pid!r}"
            )
        if not isinstance(port.get("card_count"), int) or port["card_count"] <= 0:
            errors.append(
                f"Port {port.get('port')} has invalid card_count "
                f"{port.get('card_count')!r}"
            )

    # Cross-check: each pillar's `ports` list must match the ports referencing it
    port_to_pillar = {p.get("port"): p.get("pillar") for p in ports}
    for pillar in pillars:
        for declared in pillar.get("ports", []):
            if port_to_pillar.get(declared) != pillar.get("id"):
                errors.append(
                    f"Pillar {pillar.get('id')!r} lists port {declared} "
                    f"but port maps to {port_to_pillar.get(declared)!r}"
                )

    return errors


def validate_layout(layout, config=None):
    """Return list of validation error strings, or [] if layout is valid."""
    if not isinstance(layout, dict):
        return ["Layout must be a dict"]
    errors = []
    overrides = layout.get("pillar_overrides", {})
    if not isinstance(overrides, dict):
        return ["pillar_overrides must be a dict"]

    valid_ids = None
    if config is not None:
        valid_ids = {p["id"] for p in config.get("pillars", [])}

    for pid, override in overrides.items():
        if valid_ids is not None and pid not in valid_ids:
            errors.append(f"Layout references unknown pillar {pid!r}")
        if not isinstance(override, dict):
            errors.append(f"Override for pillar {pid!r} must be a dict")
            continue
        if "position" in override:
            pos = override["position"]
            if not isinstance(pos, dict) or "x" not in pos or "y" not in pos:
                errors.append(f"Pillar {pid!r} override has invalid position")
            elif not all(isinstance(pos[k], (int, float)) for k in ("x", "y")):
                errors.append(f"Pillar {pid!r} position x/y must be numeric")
    return errors


def total_card_count(config):
    """Sum of card_count across all ports — sanity check for total wall size."""
    return sum(p.get("card_count", 0) for p in config.get("ports", []))


def get_pillar(config, pillar_id):
    """Look up a pillar by id, or return None."""
    for p in config.get("pillars", []):
        if p.get("id") == pillar_id:
            return p
    return None


def get_port(config, port_num):
    """Look up a port by number, or return None."""
    for p in config.get("ports", []):
        if p.get("port") == port_num:
            return p
    return None


def ports_for_opt(config, opt_num):
    """Return list of port numbers carried by the given OPT fiber."""
    return sorted(
        p["port"] for p in config.get("ports", []) if p.get("opt") == opt_num
    )


def get_output_card(config, card_id):
    """Look up an output card definition by its card_id (e.g. 'O-5')."""
    for c in config.get("output_cards", []):
        if c.get("card_id") == card_id:
            return c
    return None
