"""No-op stand-in for the customer DataLake ``extract_data`` Tool."""

def extract_data(
    data_type: str,
    lot_cd: str,
    process: list | str,
    query_mode: str,
    start_dt: str | None,
    end_dt: str | None,
    wafer_list: list | None,
    title: list | str | None = None,
    symbol: list | str | None = None,
    bin: list | str | int | None = None,
    device: list | str | None = None,
    owner: list | str | None = None,
    program: list | str | None = None,
    equip: list | str | None = None,
    limit: int | None = None,
    **kwargs: object,
) -> dict[str, object]:
    """Accept the customer extraction arguments and return a placeholder result.

    This function deliberately performs no file or DataLake access. It only
    preserves the planned call/return contract until the customer implementation
    replaces it. The following transform Tool supplies local wide mock data.

    Args:
        data_type: 데이터 종류 (KGSD, NCE, PKT, TDBI, WIS, WT_BIN_PARA,
            WT_ITEM_PARA, WT_SYMBOL). 필수.
        lot_cd: Lot 코드. 필수.
        process: 공정 목록 또는 공정명. 필수.
        query_mode: 조회 방식 (period 또는 wafer_list). 필수.
        start_dt: 조회 시작일. 필수 인자이며 wafer_list 조회에서는 None 전달.
        end_dt: 조회 종료일. 필수 인자이며 wafer_list 조회에서는 None 전달.
        wafer_list: Wafer ID 목록. 필수 인자이며 period 조회에서는 None 전달.
        title: Title 필터. 선택.
        symbol: Symbol 필터. 선택.
        bin: Bin 필터. 선택.
        device: Device 필터. 선택.
        owner: Owner 필터. 선택.
        program: Program 필터. 선택.
        equip: 설비 필터. 선택.
        limit: 조회 행 제한. 선택.
        kwargs: 고객 구현에서 사용하는 추가 조회 조건. 선택.

    Returns:
        추출 결과 placeholder와 전달된 query_params.
    """
    normalized_type = str(data_type).strip().lower()
    query_params = {
        "lot_cd": lot_cd,
        "process": process,
        "query_mode": query_mode,
        "start_dt": start_dt,
        "end_dt": end_dt,
        "wafer_list": wafer_list,
        "title": title,
        "symbol": symbol,
        "bin": bin,
        "device": device,
        "owner": owner,
        "program": program,
        "equip": equip,
        "limit": limit,
        **kwargs,
    }
    return {
        "data": {},
        "metadata": {
            "shape": [0, 0],
            "columns": [],
            "dtypes": {},
            "placeholder": True,
        },
        "preview_data": [],
        "shape": [0, 0],
        "columns": [],
        "cache_status": "mock",
        "file_path": "",
        "data_type": normalized_type,
        "query_params": query_params,
        "cache_key": "",
    }
