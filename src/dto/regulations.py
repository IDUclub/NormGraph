"""Zone regulations of ПЗЗ documents (see ``src/regulations``)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Section = Literal["main", "conditional", "auxiliary"]
ParameterKind = Literal[
    "max_height",
    "max_floors",
    "max_coverage",
    "plot_size",
    "setback",
    "distance",
    "min_green_share",
    "hazard_class",
    "parking",
    "other",
]


class PermittedUseOut(BaseModel):
    section: Section
    name: str
    description: str = ""
    codes: list[str] = Field(
        default_factory=list, description="ВРИ classifier codes, e.g. «2.1.1»"
    )
    only_existing: bool = Field(
        False, description="«<*>»: only for plots under existing buildings"
    )
    fragment_id: str | None = None


class ZoneParameterOut(BaseModel):
    name: str = Field(description="the row as written in the ПЗЗ")
    kind: ParameterKind
    operator: Literal["<=", ">="] | None = Field(
        None, description="how the value limits: max_* are <=, distances >="
    )
    value: float | None = Field(None, description="the single number of the row")
    values: list[float] = Field(default_factory=list)
    unit: str | None = None
    raw_value: str = ""
    not_set: bool = Field(False, description="«не подлежит установлению»")
    minimum: float | None = None
    maximum: float | None = None
    vri_codes: list[str] = Field(
        default_factory=list, description="the row applies only to these uses"
    )
    except_vri_codes: list[str] = Field(
        default_factory=list, description="the row applies to all uses but these"
    )
    building: Literal["residential", "non_residential"] | None = None
    footnote: bool = Field(False, description="marked «*»: see the zone's notes")
    number: str | None = None
    fragment_id: str | None = None


class ZoneSource(BaseModel):
    doc_id: str
    name: str
    title: str | None = None
    version: str | None = None
    territory_id: int | None = None
    territory_name: str | None = None
    effective_date: str | None = None


class ZoneRegulationOut(BaseModel):
    code: str
    name: str
    article: str | None = None
    group: str | None = None
    uses: list[PermittedUseOut] = Field(default_factory=list)
    parameters: list[ZoneParameterOut] = Field(default_factory=list)
    section_notes: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    see_articles: list[str] = Field(
        default_factory=list, description="articles of the ПЗЗ the zone refers to"
    )
    fragment_ids: list[str] = Field(default_factory=list)
    amended_by: list[str] = Field(
        default_factory=list, description="IDU_DVD acts whose changes the zone carries"
    )
    document: ZoneSource


class ZoneListResponse(BaseModel):
    count: int = 0
    zones: list[ZoneRegulationOut] = Field(default_factory=list)


class RegulationDocument(ZoneSource):
    zones: int = 0
