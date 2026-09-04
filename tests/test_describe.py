import json
from pathlib import Path

import pytest

from mtg_xianyu import describe
from mtg_xianyu.describe import (
    SHOP_POLICY,
    _classify_and_compose_finish,
    _extract_suffixes,
    build_description,
    build_finish_zh,
)

# ── _extract_suffixes ─────────────────────────────────────────────────────────

def test_extract_suffixes_no_suffix():
    base, suffixes = _extract_suffixes("Lightning Bolt")
    assert base == "Lightning Bolt"
    assert suffixes == []


def test_extract_suffixes_single():
    base, suffixes = _extract_suffixes("Imperial Seal (Borderless)")
    assert base == "Imperial Seal"
    assert suffixes == ["Borderless"]


def test_extract_suffixes_double():
    base, suffixes = _extract_suffixes("Foo (Borderless) (Foil Etched)")
    assert base == "Foo"
    assert suffixes == ["Foil Etched", "Borderless"]   # innermost first


# ── _classify_and_compose_finish ─────────────────────────────────────────────

def test_normal_foil():
    result = _classify_and_compose_finish([], "Foil", "Lightning Bolt")
    assert result == "英文闪"


def test_normal_normal():
    result = _classify_and_compose_finish([], "Normal", "Lightning Bolt")
    assert result == "英文平"


def test_borderless_foil():
    result = _classify_and_compose_finish(["Borderless"], "Foil", "Imperial Seal (Borderless)")
    assert result == "异画英文闪"


def test_borderless_normal():
    result = _classify_and_compose_finish(["Borderless"], "Normal", "Imperial Seal (Borderless)")
    assert result == "异画英文平"


def test_extended_art_foil():
    result = _classify_and_compose_finish(["Extended Art"], "Foil", "Foo (Extended Art)")
    assert result == "扩画英文闪"


def test_retro_frame_normal():
    result = _classify_and_compose_finish(["Retro Frame"], "Normal", "Esper Sentinel (Retro Frame)")
    assert result == "老框英文平"


def test_showcase_maps_to_yihua():
    result = _classify_and_compose_finish(["Showcase"], "Foil", "Foo (Showcase)")
    assert result == "异画英文闪"


def test_foil_etched():
    result = _classify_and_compose_finish(["Foil Etched"], "Foil", "Foo (Foil Etched)")
    assert result == "英文蚀刻闪"


def test_rainbow_foil():
    result = _classify_and_compose_finish(["Rainbow Foil"], "Foil", "Foo (Rainbow Foil)")
    assert result == "英文彩虹闪"


def test_surge_foil():
    result = _classify_and_compose_finish(["Surge Foil"], "Foil", "Foo (Surge Foil)")
    assert result == "英文潮涌闪"


@pytest.mark.parametrize(
    ("name_en", "printing", "expected"),
    [
        ("Necropotence (Anime Borderless)", "Normal", "动漫无边框英文平"),
        ("Urza's Saga (White Border)", "Normal", "白边英文平"),
        ("City of Brass (Future Sight)", "Normal", "未来框英文平"),
        (
            "Zopandrel, Hunger Dominus (Oil Slick Raised Foil)",
            "Foil",
            "英文油膜浮雕闪",
        ),
        ("Negate (JP Alternate Art)", "Foil", "日文异画闪"),
        (
            "Phyrexian Arena (Phyrexian) (ONE Bundle)",
            "Foil",
            "非瑞克西亚文闪",
        ),
    ],
)
def test_current_collection_special_treatments(name_en, printing, expected):
    assert build_finish_zh(name_en, printing) == expected


def test_borderless_and_foil_etched():
    # innermost suffix is "Foil Etched", outermost is "Borderless"
    suffixes = ["Foil Etched", "Borderless"]
    result = _classify_and_compose_finish(suffixes, "Foil", "Foo (Borderless) (Foil Etched)")
    assert result == "异画英文蚀刻闪"


def test_finish_variant_with_normal_printing_warns(capsys):
    result = _classify_and_compose_finish(["Foil Etched"], "Normal", "Foo (Foil Etched)")
    err = capsys.readouterr().err
    assert "Foil Etched" in err
    assert "Normal" in err
    assert result == "英文蚀刻闪"     # still applied despite warning


def test_set_code_suffix_stripped_silently(capsys):
    for suffix in ["DVD", "IMA", "A25", "2X2"]:
        result = _classify_and_compose_finish([suffix], "Foil", f"Foo ({suffix})")
        err = capsys.readouterr().err
        assert err == "", f"unexpected warning for suffix '{suffix}'"
        assert result == "英文闪"


def test_number_suffix_stripped_silently(capsys):
    for suffix in ["350", "280", "1553"]:
        result = _classify_and_compose_finish([suffix], "Normal", f"Foo ({suffix})")
        err = capsys.readouterr().err
        assert err == "", f"unexpected warning for suffix '{suffix}'"
        assert result == "英文平"


def test_unknown_suffix_warns_and_defaults(capsys):
    result = _classify_and_compose_finish(["Unknown Thing"], "Foil", "Foo (Unknown Thing)")
    err = capsys.readouterr().err
    assert "Unknown Thing" in err
    assert result == "英文闪"          # defaults to base finish with no visual prefix


@pytest.mark.parametrize(
    "name_en",
    [
        "Reflections of Littjara (KHM Bundle)",
        "Post's Sigil - Leshrac's Sigil (Post Malone)",
    ],
)
def test_edition_metadata_suffixes_are_silent(name_en, capsys):
    assert build_finish_zh(name_en, "Foil") == "英文闪"
    assert capsys.readouterr().err == ""


# ── build_description ─────────────────────────────────────────────────────────

def test_name_en_strips_parens_in_line2():
    listing = {
        "name_en": "Imperial Seal (Borderless)",
        "name_zh": "玉玺",
        "printing": "Normal",
        "set_name_zh": "双星大师2022",
        "set_code": "2X2",
    }
    lines = build_description(listing).split("\n")
    assert lines[1] == "Imperial Seal"


def test_shop_policy_appears_last_line():
    listing = {
        "name_en": "Lightning Bolt",
        "name_zh": "闪电击",
        "printing": "Normal",
        "set_name_zh": "第四版",
        "set_code": "4ED",
    }
    assert build_description(listing).split("\n")[-1] == SHOP_POLICY


def test_full_description_jetmir():
    listing = {
        "name_en": "Jetmir's Garden",
        "name_zh": "杰米尔的花园",
        "printing": "Foil",
        "set_name_zh": "新卡佩纳：喧嚣黑街",
        "set_code": "SNC",
    }
    expected = (
        "万智牌 MTG 杰米尔的花园\n"
        "Jetmir's Garden\n"
        "新卡佩纳：喧嚣黑街/SNC 英文闪\n"
        "主页满300包邮"
    )
    assert build_description(listing) == expected


# ── CLI ──────────────────────────────────────────────────────────────────────────

def _write_approved_listing(
    listings_dir: Path,
    *,
    row_id: str = "1_0",
    name_en: str = "Test Card (Borderless)",
    jpg_finish: str = "异画英文平",
) -> tuple[Path, Path, Path]:
    listings_dir.mkdir(parents=True, exist_ok=True)
    jpg_path = listings_dir / f"TST-1 - Test Card - {jpg_finish}.jpg"
    jpg_path.write_bytes(b"jpeg")
    listing = {
        "row_id": row_id,
        "name_en": name_en,
        "name_zh": "测试牌",
        "printing": "Normal",
        "set_name_zh": "测试系列",
        "set_code": "TST",
        "collector_number": "1",
        "photo_jpg": str(jpg_path),
    }
    json_path = listings_dir / f"{row_id}.json"
    json_path.write_text(json.dumps(listing), encoding="utf-8")
    txt_path = listings_dir / f"{row_id}.txt"
    txt_path.write_text("stale", encoding="utf-8")
    return json_path, jpg_path, txt_path


def test_main_refuses_treatment_drift_without_mutating(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    json_path, old_jpg, txt_path = _write_approved_listing(
        Path("data/listings"), jpg_finish="英文平"
    )
    original_json = json_path.read_text(encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        describe.main()

    assert exc_info.value.code == 1
    assert "mtg-reconcile --apply" in capsys.readouterr().err
    assert json_path.read_text(encoding="utf-8") == original_json
    assert txt_path.read_text(encoding="utf-8") == "stale"
    assert old_jpg.is_file()
    assert not old_jpg.with_name("TST-1 - Test Card - 异画英文平.jpg").exists()


def test_main_updates_text_when_jpeg_name_is_current(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _, jpg_path, txt_path = _write_approved_listing(Path("data/listings"))

    describe.main()

    assert "异画英文平" in txt_path.read_text(encoding="utf-8")
    assert jpg_path.is_file()
    assert "Wrote 1 changed description(s)." in capsys.readouterr().out


def test_main_preflights_all_listings_before_writing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _, _, txt_path = _write_approved_listing(Path("data/listings"))
    malformed_path = Path("data/listings/9_0.json")
    malformed_path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(ValueError, match="cannot load approved listing"):
        describe.main()

    assert txt_path.read_text(encoding="utf-8") == "stale"
