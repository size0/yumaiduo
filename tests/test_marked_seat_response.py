from integrations.liangpiao.api import RecognitionResult


def test_raw_marked_seats_are_retained_when_final_result_drops_them():
    result = RecognitionResult.from_data({
        "rawResults": {"seat": [{"seatNo": "5排11座"}]},
        "finalResults": {"seat": [], "matchLevel": "EXACT"},
    })
    assert [row["seatNo"] for row in result.seats] == ["5排11座"]


def test_unrelated_raw_geometry_is_not_treated_as_seat():
    result = RecognitionResult.from_data({
        "rawResults": {"seat": [{"rowNo": 5, "colNo": 11}]},
        "finalResults": {"seat": [], "matchLevel": "EXACT"},
    })
    assert result.seats == []
