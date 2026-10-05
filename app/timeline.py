"""把货物自有事件与车载测温读数合并为统一读数时间线。

合并规则
--------
* 车载读数仅对“读数时刻仍在车上”的货物生效——该货物在对应车次登记了装卸时段
  ``[loaded_at, unloaded_at)`` 且 ``loaded_at <= 读数时刻 < unloaded_at``（左闭右开）。
* 同一时刻该货物有自有测温时，**优先采用自有测温**；车载读数在该时刻对该货物
  标记 ``adopted=False``（被取代），不参与越温重建，但原始读数仍可查询。
* **生效探头故障覆盖的车载读数一律隔离**：读数所属车次存在未撤销故障且读数
  时刻落在其左闭右开故障时段内时，该读数对在车货物标记 ``adopted=False`` /
  ``quarantined=True`` 并附覆盖故障编号，不参与越温重建（原始读数仍可查）；
  撤销最后一条覆盖故障后恢复按当前有效温度采用（同刻自有测温仍优先）。
* 交接事件只来自货物自有事件，车载读数从不改变保管方。
* **测温更正生效于有效温度**：测温点的温度取该自有测温 / 车载读数最新一次
  更正后的有效温度（无更正时为原始温度）。同刻自有测温优先仍只取决于“该时刻
  是否存在自有测温”，与温度取值无关；被取代 / 被隔离的车载点同样携带更正后的
  有效温度与版本（仅用于展示，不参与重建）。
"""
from __future__ import annotations

from decimal import Decimal

from app.chain import (
    SOURCE_OWN,
    SOURCE_VEHICLE,
    TimelinePoint,
    point_from_event,
)


def is_on_board(loading, at) -> bool:
    """读数时刻是否仍在车上：左闭右开 [loaded_at, unloaded_at)。"""
    return loading.loaded_at <= at < loading.unloaded_at


def is_reading_applicable(loading, reading) -> bool:
    """车载读数是否对某票货物生效：同一车次且读数时刻仍在车上（左闭右开）。

    必须同时匹配车次与时刻：同一货物可在首尾相接的不同车次上各有装卸时段，
    不能因另一车次同刻开始的窗口而把本车次已下车时刻的读数误判为适用。
    """
    return loading.trip_id == reading.trip_id and is_on_board(
        loading, reading.read_at
    )


def onboard_loadings(loadings, at) -> list:
    """返回 at 时刻车上的装卸登记（同货物同刻至多一条，应用侧已校验不重叠）。"""
    return [l for l in loadings if is_on_board(l, at)]


def merged_timeline(
    own_events,
    vehicle_readings,
    loadings,
    event_effective: dict[str, tuple] | None = None,
    reading_effective: dict[str, tuple] | None = None,
    reading_quarantine: dict[str, tuple] | None = None,
    standards: tuple = (),
) -> list[TimelinePoint]:
    """生成货物的完整读数时间线（含被取代 / 被隔离的车载点，按时间升序）。

    :param own_events: 该货物的全部自有事件（测温 / 交接）。
    :param vehicle_readings: 该货物所有车次的车载原始读数（可含与其无关车次的，
        由 loadings 决定是否适用）。
    :param loadings: 该货物的全部装卸时段登记。
    :param event_effective: 自有测温事件ID -> ``(有效温度, 版本)``，
        仅含发生过更正的测温；缺失即原始温度、版本 0。
    :param reading_effective: 车载读数ID -> ``(有效温度, 版本)``，语义同上。
    :param reading_quarantine: 车载读数ID -> 覆盖该读数的**生效探头故障**
        编号元组（仅被覆盖的读数有键）。被覆盖的在车读数标记隔离、不参与
        重建；覆盖故障编号随点输出。隔离与“同刻自有测温优先”相互独立——
        同刻有自有测温的读数即使也被故障覆盖，仍标 ``quarantined=True``
        但 adopted 的首要原因是被自有测温取代。
    :param standards: 货物完整温度标准历程
        （:class:`app.standards.StandardEntry` 元组，含初始版本）。每个测温点
        按“生效时刻 <= 读数时刻的最新一条”标注其**当时生效上限**
        （左闭，边界读数取新上限）；为空时不标注（重建退化为初始上限）。
    """
    event_effective = event_effective or {}
    reading_effective = reading_effective or {}
    reading_quarantine = reading_quarantine or {}

    def _limit_at(at):
        if not standards:
            return None
        from app.standards import limit_at

        return limit_at(standards, at)

    points: list[TimelinePoint] = []
    own_measure_at = set()
    for e in own_events:
        p = point_from_event(e)
        if p.is_measurement:
            own_measure_at.add(e.occurred_at)
            eff = event_effective.get(e.event_id)
            if eff is not None:
                p = TimelinePoint(
                    occurred_at=p.occurred_at,
                    is_measurement=True,
                    temperature=Decimal(eff[0]),
                    source=SOURCE_OWN,
                    source_id=e.event_id,
                    adopted=True,
                    temp_version=eff[1],
                    temp_limit=_limit_at(e.occurred_at),
                )
            else:
                p = TimelinePoint(
                    occurred_at=p.occurred_at,
                    is_measurement=True,
                    temperature=Decimal(e.temperature),
                    source=SOURCE_OWN,
                    source_id=e.event_id,
                    adopted=True,
                    temp_limit=_limit_at(e.occurred_at),
                )
        points.append(p)

    for r in vehicle_readings:
        # 必须在“同一车次”的装卸时段内且读数时刻仍在车上
        applicable = any(is_reading_applicable(l, r) for l in loadings)
        if not applicable:
            # 与该货物无关的车次读数（不在车 / 别的车次）完全不出现在其时间线上
            continue
        eff = reading_effective.get(r.reading_id)
        if eff is not None:
            temperature, r_version = Decimal(eff[0]), eff[1]
        else:
            temperature, r_version = Decimal(r.temperature), 0
        covers = tuple(reading_quarantine.get(r.reading_id, ()))
        points.append(
            TimelinePoint(
                occurred_at=r.read_at,
                is_measurement=True,
                temperature=temperature,
                source=SOURCE_VEHICLE,
                source_id=r.reading_id,
                trip_id=r.trip_id,
                # 同刻自有测温优先；被生效故障覆盖的读数也不参与重建
                adopted=r.read_at not in own_measure_at and not covers,
                temp_version=r_version,
                quarantine_faults=covers,
                temp_limit=_limit_at(r.read_at),
            )
        )
    points.sort(
        key=lambda p: (
            p.occurred_at,
            0 if p.is_measurement else 1,
            {SOURCE_OWN: 0, SOURCE_VEHICLE: 1}.get(p.source, 9),
            p.source_id or "",
        )
    )
    return points


def adopted_timeline(
    own_events,
    vehicle_readings,
    loadings,
    event_effective: dict[str, tuple] | None = None,
    reading_effective: dict[str, tuple] | None = None,
    reading_quarantine: dict[str, tuple] | None = None,
    standards: tuple = (),
) -> list[TimelinePoint]:
    """只含被采用点的时间线，供保管链重建使用。"""
    return [
        p
        for p in merged_timeline(
            own_events,
            vehicle_readings,
            loadings,
            event_effective,
            reading_effective,
            reading_quarantine,
            standards,
        )
        if p.adopted
    ]


def load_cargo_context(db, cargo_id: str):
    """一次读齐货物的自有事件、装卸时段、相关车次车载读数、测温更正有效值、
    生效探头故障隔离映射与**完整温度标准历程**。

    返回 ``(events, loadings, readings, effective, quarantine, standards)``；
    ``effective`` 为 ``(event_effective, reading_effective)`` 两个映射，键分别
    为自有测温事件ID与车载读数ID，值为 ``(有效温度, 当前版本)``，只含发生过
    更正的目标；``quarantine`` 为车载读数ID -> 覆盖该读数的生效故障编号元组
    （仅含被该读数所属车次生效故障覆盖的读数；是否在车由合并阶段按装卸时段
    再判定）；``standards`` 为含初始版本的标准历程
    （:class:`app.standards.StandardEntry` 元组，按生效时刻升序）。
    """
    cargos, contexts = load_cargo_context_batch(db, [cargo_id])
    if cargo_id not in contexts:
        from app.crud import ApiError

        raise ApiError(404, "cargo_not_found", f"货物 {cargo_id} 不存在")
    return contexts[cargo_id]


def load_cargo_context_batch(db, cargo_ids):
    """**按批次**一次读齐多票货物的完整重建上下文（车次同时越温查询专用）。

    与逐票调用 :func:`load_cargo_context` 语义一致——每票货物仍纳入其
    **全部车次**历史（货物可能从上一车次带着未闭合越温区间装到查询车次，
    见 :mod:`app.chain`）——但所有读取均按集合下发：一批货物固定约 9 条 SQL，
    数据库往返随**批次数**（``ceil(货物数 / 批大小)``）而非逐票增长。调用方
    负责分批，整个调用应处于同一事务 / 同一已提交快照内（见
    :func:`app.vehicle_crud.query_simultaneous_excursions`，PG 下为
    REPEATABLE READ 快照）。

    同时满足“**不加载无关车次数据**”：车载读数只取“批次内至少一票货物在
    读数时刻仍在其车次车上”的行（SQL EXISTS 按装卸时段左闭右开
    ``loaded_at <= read_at < unloaded_at`` 判定），与批次货物装卸时段毫无
    交集的车次读数不会下发；生效探头故障同样只查这些相关车次。每票货物的
    自有事件、装卸时段、测温更正与温度标准历程则限定在本批货物范围内。

    返回 ``(cargos, contexts)``：

    * ``cargos``：编号 -> Cargo 行（外键约束下批内货物都存在；防御性仍可能
      缺键，缺键货物不出现在 ``contexts`` 中）；
    * ``contexts``：编号 ->
      ``(events, loadings, readings, effective, quarantine, standards)``，
      各分量结构与 :func:`load_cargo_context` 相同，其中 ``effective`` /
      ``quarantine`` 为批次共享映射（多出的键对各自货物重建无影响——合并
      时间线只按该货物时间线上的点查表）。
    """
    from sqlalchemy import select

    from app import faults as faults_mod
    from app import standards as std_mod
    from app.models import Cargo, Event, Loading, TempStandardChange, VehicleReading
    from app.timeutil import ensure_utc

    cargo_ids = sorted(set(cargo_ids))
    cargos = {
        c.cargo_id: c
        for c in db.scalars(
            select(Cargo).where(Cargo.cargo_id.in_(cargo_ids or [""]))
        ).all()
    }
    live_ids = sorted(cargos)

    # 1) 本批货物的全部装卸时段（含跨车次历史，仅这些货物）
    all_loadings = list(
        db.scalars(
            select(Loading)
            .where(Loading.cargo_id.in_(live_ids or [""]))
            .order_by(Loading.loaded_at, Loading.trip_id, Loading.cargo_id)
        ).all()
    )
    for l in all_loadings:
        l.loaded_at = ensure_utc(l.loaded_at)
        l.unloaded_at = ensure_utc(l.unloaded_at)
    loadings_by_cargo: dict[str, list] = {}
    trip_ids = set()
    for l in all_loadings:
        loadings_by_cargo.setdefault(l.cargo_id, []).append(l)
        trip_ids.add(l.trip_id)

    # 2) 本批货物的全部自有事件（测温 / 交接，按既有顺序）
    all_events = list(
        db.scalars(
            select(Event)
            .where(Event.cargo_id.in_(live_ids or [""]))
            .order_by(Event.occurred_at, Event.event_id)
        ).all()
    )
    for e in all_events:
        e.occurred_at = ensure_utc(e.occurred_at)
    events_by_cargo: dict[str, list] = {}
    for e in all_events:
        events_by_cargo.setdefault(e.cargo_id, []).append(e)

    # 3) 相关车次车载读数：只要批次内存在某票货物在读数时刻仍在该车次车上
    #    （左闭右开）。无任何交集的车次读数一律不下发——不加载无关车次数据。
    readings: list = []
    quarantine: dict[str, tuple] = {}
    if all_loadings:
        onboard = (
            select(Loading.cargo_id)
            .where(
                Loading.trip_id == VehicleReading.trip_id,
                Loading.loaded_at <= VehicleReading.read_at,
                Loading.unloaded_at > VehicleReading.read_at,
            )
            .exists()
            .correlate(VehicleReading)
        )
        readings = list(
            db.scalars(
                select(VehicleReading)
                .where(VehicleReading.trip_id.in_(sorted(trip_ids)), onboard)
                .order_by(VehicleReading.read_at, VehicleReading.reading_id)
            ).all()
        )
        for r in readings:
            r.read_at = ensure_utc(r.read_at)

        # 4) 相关车次的生效探头故障：覆盖的车载读数一律隔离（与在车判定独立）
        active = faults_mod.load_active_faults(db, trip_ids)
        quarantine = faults_mod.reading_quarantine_map(readings, active)

    # 5) 测温更正有效值：自有事件 / 车载读数各一条集合查询（批次共享映射）
    effective = load_effective_maps(
        db,
        event_ids={
            e.event_id
            for e in all_events
            if getattr(e.event_type, "name", e.event_type) == "measurement"
        },
        reading_ids={r.reading_id for r in readings},
    )

    # 6) 温度标准变更：本批货物一次查齐，内存按货物组装完整历程
    change_rows = list(
        db.scalars(
            select(TempStandardChange)
            .where(TempStandardChange.cargo_id.in_(live_ids or [""]))
            .order_by(TempStandardChange.effective_at, TempStandardChange.change_id)
        ).all()
    )
    for r in change_rows:
        r.effective_at = ensure_utc(r.effective_at)
    changes_by_cargo: dict[str, list] = {}
    for r in change_rows:
        changes_by_cargo.setdefault(r.cargo_id, []).append(r)

    # 每票货物在内存分组（含其全部车次历史）；读数按“同车次且在车”过滤，
    # 保持 merged_timeline 的适用性口径，也避免逐票重复全表扫描。
    contexts: dict[str, tuple] = {}
    for cid in live_ids:
        cargo = cargos[cid]
        cargo.started_at = ensure_utc(cargo.started_at)
        cargo_loadings = loadings_by_cargo.get(cid, [])
        applicable_trips = {l.trip_id for l in cargo_loadings}
        cargo_readings = [
            r
            for r in readings
            if r.trip_id in applicable_trips
            and any(is_reading_applicable(l, r) for l in cargo_loadings)
        ]
        standards = std_mod.build_series(
            cargo, changes_by_cargo.get(cargo.cargo_id, [])
        )
        contexts[cid] = (
            events_by_cargo.get(cid, []),
            cargo_loadings,
            cargo_readings,
            effective,
            quarantine,
            standards,
        )
    return cargos, contexts


def load_effective_maps(db, event_ids=None, reading_ids=None):
    """读取目标测温的当前有效值：``(event_effective, reading_effective)``。

    每个映射的值为 ``(Decimal 有效温度, int 当前版本)``，只含存在更正的目标。
    同一目标取 ``version`` 最大（= 最新一次更正）的一条。
    """
    from sqlalchemy import select

    from app.models import CorrectionTarget, TemperatureCorrection

    event_effective: dict[str, tuple] = {}
    reading_effective: dict[str, tuple] = {}

    if event_ids:
        rows = db.scalars(
            select(TemperatureCorrection)
            .where(
                TemperatureCorrection.target_type
                == CorrectionTarget.own_event,
                TemperatureCorrection.target_event_id.in_(event_ids),
            )
        ).all()
        latest: dict[str, TemperatureCorrection] = {}
        for row in rows:
            prev = latest.get(row.target_event_id)
            if prev is None or row.version > prev.version:
                latest[row.target_event_id] = row
        event_effective = {
            eid: (Decimal(row.new_temperature), row.version)
            for eid, row in latest.items()
        }

    if reading_ids:
        rows = db.scalars(
            select(TemperatureCorrection)
            .where(
                TemperatureCorrection.target_type
                == CorrectionTarget.vehicle_reading,
                TemperatureCorrection.target_reading_id.in_(reading_ids),
            )
        ).all()
        latest = {}
        for row in rows:
            prev = latest.get(row.target_reading_id)
            if prev is None or row.version > prev.version:
                latest[row.target_reading_id] = row
        reading_effective = {
            rid: (Decimal(row.new_temperature), row.version)
            for rid, row in latest.items()
        }

    return event_effective, reading_effective


def resolve_trip_for_reading(db, trip_id: str):
    """取车次行（供读数适用性判断等场景）。"""
    from app.models import Trip

    return db.get(Trip, trip_id)
