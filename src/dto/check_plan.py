"""Strict CheckPlan v1 contract embedded into restriction responses."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


RoleName = Annotated[
    str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
]


class LayerRequirement(StrictModel):
    role: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    entity: str = Field(min_length=1, max_length=200)
    entity_type: Literal["service", "physical_object", "functional_zone"]
    geometry_types: list[
        Literal[
            "Point",
            "MultiPoint",
            "LineString",
            "MultiLineString",
            "Polygon",
            "MultiPolygon",
        ]
    ] = Field(default_factory=list, max_length=6)
    required: bool = True


class AttributeCandidate(StrictModel):
    field: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-zА-Яа-яЁё0-9_.:-]+$",
    )
    unit: str = Field(min_length=1, max_length=32)
    derive: Literal["height_to_floors_v1"] | None = None
    quality: Literal["direct", "derived"]

    @model_validator(mode="after")
    def derivation_matches_quality(self) -> "AttributeCandidate":
        if (self.derive is None) != (self.quality == "direct"):
            raise ValueError("derive is required exactly for derived candidates")
        return self


class AttributeRequirement(StrictModel):
    role: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    on: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    required: bool = True
    accepts: list[AttributeCandidate] = Field(min_length=1, max_length=12)
    min_fill_rate: float = Field(default=1.0, ge=0, le=1)


class DeclaredRequirements(StrictModel):
    layers: list[LayerRequirement] = Field(default_factory=list, max_length=16)
    attributes: list[AttributeRequirement] = Field(default_factory=list, max_length=24)

    @model_validator(mode="after")
    def roles_are_unique_and_referential(self) -> "DeclaredRequirements":
        layer_roles = [item.role for item in self.layers]
        attribute_roles = [item.role for item in self.attributes]
        if len(layer_roles) != len(set(layer_roles)):
            raise ValueError("layer requirement roles must be unique")
        if len(attribute_roles) != len(set(attribute_roles)):
            raise ValueError("attribute requirement roles must be unique")
        unknown = sorted({item.on for item in self.attributes} - set(layer_roles))
        if unknown:
            raise ValueError(
                f"attribute requirements reference unknown layer roles: {unknown}"
            )
        return self


class CheckPlanSource(StrictModel):
    restriction_id: str = Field(min_length=1, max_length=128)
    document_name: str | None = Field(default=None, max_length=300)
    clause_number: str | None = Field(default=None, max_length=100)
    extraction_text: str | None = Field(default=None, max_length=8000)


class CheckPlan(StrictModel):
    schema_version: Literal["1.0"]
    template: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    template_version: int = Field(ge=1, le=1000)
    params: dict[str, Any]
    declared_requirements: DeclaredRequirements | None = None
    source: CheckPlanSource
    planner_status: Literal["auto", "reviewed", "unsupported"]


class DistanceFromSourceParams(StrictModel):
    source_layer: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    targets: list[RoleName] = Field(min_length=1, max_length=16)
    geometry_mode: Literal["buffered", "source_geometry"]
    predicate: Literal["intersects", "within", "contains"]
    violation_when: Literal["matched", "not_matched"]
    result_mode: Literal["violated", "passed", "both"] = "both"
    distance_m: float | None = Field(default=None, gt=0, le=100_000)

    @model_validator(mode="after")
    def buffered_mode_requires_distance(self) -> "DistanceFromSourceParams":
        if self.geometry_mode == "buffered" and self.distance_m is None:
            raise ValueError("distance_m is required for buffered geometry")
        if self.geometry_mode == "source_geometry" and self.distance_m is not None:
            raise ValueError("distance_m is forbidden for source_geometry")
        if len(self.targets) != len(set(self.targets)):
            raise ValueError("targets must be unique")
        return self


class DistanceBand(StrictModel):
    min: float = Field(ge=-1_000_000, le=1_000_000)
    max: float | None = Field(default=None, ge=-1_000_000, le=1_000_000)
    distance_m: float = Field(gt=0, le=100_000)

    @model_validator(mode="after")
    def valid_bounds(self) -> "DistanceBand":
        if self.max is not None and self.max < self.min:
            raise ValueError("band max must be greater than or equal to min")
        return self


class DistanceTableParams(StrictModel):
    source_layer: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    attribute_role: str = Field(
        min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$"
    )
    bands: list[DistanceBand] = Field(min_length=1, max_length=50)
    targets: list[RoleName] = Field(min_length=1, max_length=16)
    predicate: Literal["intersects", "within", "contains"] = "intersects"
    violation_when: Literal["matched", "not_matched"] = "matched"
    result_mode: Literal["violated", "passed", "both"] = "both"
    null_policy: Literal["unchecked"] = "unchecked"
    out_of_range_policy: Literal["unchecked"] = "unchecked"

    @model_validator(mode="after")
    def bands_are_ordered_and_unambiguous(self) -> "DistanceTableParams":
        previous_max: float | None = None
        for index, band in enumerate(self.bands):
            if index and previous_max is None:
                raise ValueError("only the last band may have max=null")
            if previous_max is not None and band.min <= previous_max:
                raise ValueError("bands must be ordered and must not overlap")
            previous_max = band.max
        return self


class PresenceWithinParams(StrictModel):
    objects_layer: str = Field(
        min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$"
    )
    required_neighbor_layers: list[RoleName] = Field(min_length=1, max_length=16)
    distance_m: float = Field(gt=0, le=100_000)
    minimum_neighbors: int = Field(default=1, ge=1, le=1000)
    result_mode: Literal["violated", "passed", "both"] = "both"


class ConstantThreshold(StrictModel):
    kind: Literal["constant"]
    value: float = Field(ge=-1_000_000_000, le=1_000_000_000)
    unit: str = Field(min_length=1, max_length=32)


class ZoneAttributeThreshold(StrictModel):
    kind: Literal["attribute_role"]
    role: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")


class ZonalAttributeThresholdParams(StrictModel):
    objects_layer: str = Field(
        min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$"
    )
    zones_layer: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    attribute_role: str = Field(
        min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$"
    )
    operator: Literal["<", "<=", ">", ">=", "=="]
    threshold_source: ConstantThreshold | ZoneAttributeThreshold
    join_predicate: Literal["intersects", "within", "contains"] = "intersects"
    multiple_zone_policy: Literal["strictest_threshold"] = "strictest_threshold"
    result_mode: Literal["violated", "passed", "both"] = "both"


class RatioNumerator(StrictModel):
    layer: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    measure: Literal["area"]


class RatioDenominator(StrictModel):
    measure: Literal["zone_area"]


class ZonalRatioParams(StrictModel):
    zones_layer: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    numerator: RatioNumerator
    denominator: RatioDenominator
    operator: Literal["<", "<=", ">", ">=", "=="]
    threshold: float = Field(ge=0, le=100)
    unit: Literal["%"] = "%"
    exclusions: list[Literal["exclude_invalid_geometry_v1"]] = Field(
        default_factory=list, max_length=4
    )
    invalid_geometry_policy: Literal["repair", "reject"] = "repair"
    result_mode: Literal["violated", "passed", "both"] = "both"


PARAM_MODELS: dict[str, type[BaseModel]] = {
    "distance_from_source": DistanceFromSourceParams,
    "distance_table": DistanceTableParams,
    "presence_within": PresenceWithinParams,
    "zonal_attribute_threshold": ZonalAttributeThresholdParams,
    "zonal_ratio": ZonalRatioParams,
}


def _validate_declared_role_references(plan: CheckPlan, params: BaseModel) -> None:
    requirements = plan.declared_requirements
    if requirements is None:
        raise ValueError("declared_requirements are required for executable plans")

    layer_roles = {item.role for item in requirements.layers}
    attribute_roles = {item.role for item in requirements.attributes}
    used_layers: set[str]
    used_attributes: set[str] = set()

    if isinstance(params, DistanceFromSourceParams):
        used_layers = {params.source_layer, *params.targets}
    elif isinstance(params, DistanceTableParams):
        used_layers = {params.source_layer, *params.targets}
        used_attributes = {params.attribute_role}
    elif isinstance(params, PresenceWithinParams):
        used_layers = {params.objects_layer, *params.required_neighbor_layers}
    elif isinstance(params, ZonalAttributeThresholdParams):
        used_layers = {params.objects_layer, params.zones_layer}
        used_attributes = {params.attribute_role}
        if isinstance(params.threshold_source, ZoneAttributeThreshold):
            used_attributes.add(params.threshold_source.role)
    elif isinstance(params, ZonalRatioParams):
        used_layers = {params.zones_layer, params.numerator.layer}
    else:  # pragma: no cover - PARAM_MODELS is the closed v1 manifest
        raise ValueError(f"unsupported params model: {type(params).__name__}")

    unknown_layers = sorted(used_layers - layer_roles)
    if unknown_layers:
        raise ValueError(f"params reference unknown layer roles: {unknown_layers}")
    unknown_attributes = sorted(used_attributes - attribute_roles)
    if unknown_attributes:
        raise ValueError(
            f"params reference unknown attribute roles: {unknown_attributes}"
        )


def validate_check_plan(value: dict[str, Any]) -> CheckPlan:
    plan = CheckPlan.model_validate(value)
    if plan.template_version != 1 or plan.template not in PARAM_MODELS:
        raise ValueError(
            f"unsupported template: {plan.template}@v{plan.template_version}"
        )
    params = PARAM_MODELS[plan.template].model_validate(plan.params)
    _validate_declared_role_references(plan, params)
    return plan


class CheckPlanReviewRequest(StrictModel):
    action: Literal["approve", "reject", "replace"]
    plan: CheckPlan | None = None
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def replace_requires_plan(self) -> "CheckPlanReviewRequest":
        if (self.action == "replace") != (self.plan is not None):
            raise ValueError("plan is required exactly for replace")
        return self


class CheckPlanReviewItem(StrictModel):
    restriction_id: str
    plan: CheckPlan
    revision: int
    review_status: Literal["pending", "approved", "rejected"]
    author: str | None = None
    reason: str | None = None
    created_at: str | None = None
    current: bool
