import settings


def _sections(cfg):
    return {section["key"]: section
            for group in cfg.layout()
            for section in group["sections"]}


def test_price_mismatch_is_a_separate_two_control_section():
    sections = _sections(settings.Settings())
    assert sections["recognize_quote"]["count"] == 4
    assert sections["price_mismatch"]["count"] == 2
    assert {c["key"] for c in sections["price_mismatch"]["controls"]} == {"mode", "message"}


def test_legacy_price_mismatch_settings_are_migrated():
    cfg = settings.Settings()
    cfg.apply_dict({"recognize_quote": {
        "price_mismatch_mode": "按识别成功处理",
        "price_mismatch_message": "请重新选座后发图",
    }})
    assert cfg.price_mismatch.mode == "按识别成功处理"
    assert cfg.price_mismatch.message == "请重新选座后发图"


def test_new_price_mismatch_settings_round_trip_without_legacy_keys():
    cfg = settings.Settings()
    cfg.apply_dict({"price_mismatch": {
        "mode": "按识别成功处理",
        "message": "请核对座位价格",
    }})
    saved = cfg.to_dict()
    assert saved["price_mismatch"] == {
        "mode": "按识别成功处理", "message": "请核对座位价格"}
    assert "price_mismatch_mode" not in saved["recognize_quote"]

