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
    # Registered deterministic conversions (executed by the compliance data gate):
    # height_to_floors_v1 — metres to floors (3 m per floor, rounded down, minimum 1);
    # floors_to_height_v1 — floors to metres (3 m per floor);
    # geometry_area_m2_v1 — polygon area in a local metric CRS (``field`` is "geometry").
    derive: (
        Literal["height_to_floors_v1", "floors_to_height_v1", "geometry_area_m2_v1"]
        | None
    ) = None
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


class CheckPlanApplicability(StrictModel):
    """How a clause with conditions or case-dependent values became one plan.

    ``strictest_variant``: the strictest of the clause's values is applied to every
    checked object, whatever its conditions. The verdict is conservative and the
    conditions must still be checked by a person.
    """

    mode: Literal["strictest_variant"]
    conditions: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        default_factory=list, max_length=20
    )
    variants: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        default_factory=list, max_length=20
    )
    applied: str = Field(min_length=1, max_length=500)


class CheckPlanScope(StrictModel):
    """Only the objects of one layer meeting a clause's condition are checked.

    The condition is a range of a numeric attribute of those objects («при
    многоэтажной застройке» — residential buildings of 9 floors and more). Objects
    without a value cannot be placed in or out of the range and are not checked.
    """

    layer: RoleName
    attribute: RoleName
    min: float | None = Field(default=None, ge=0, le=1000)
    max: float | None = Field(default=None, ge=0, le=1000)
    condition: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def range_is_bounded(self) -> "CheckPlanScope":
        if self.min is None and self.max is None:
            raise ValueError("scope needs min or max")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("scope min exceeds max")
        return self


class CheckPlan(StrictModel):
    schema_version: Literal["1.0"]
    template: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    template_version: int = Field(ge=1, le=1000)
    params: dict[str, Any]
    declared_requirements: DeclaredRequirements | None = None
    source: CheckPlanSource
    planner_status: Literal["auto", "reviewed", "unsupported"]
    applicability: CheckPlanApplicability | None = None
    scope: CheckPlanScope | None = None


class CheckPlanBackfillRequest(StrictModel):
    """One bounded, resumable page of missing CheckPlans."""

    limit: int = Field(default=100, ge=1, le=500)
    after_id: str | None = Field(default=None, min_length=1, max_length=128)
    dry_run: bool = False


class CheckPlanBackfillFailure(StrictModel):
    restriction_id: str
    error: str


class CheckPlanBackfillResponse(StrictModel):
    selected: int
    generated: int
    auto: int
    unsupported: int
    skipped: int
    failed: int
    failures: list[CheckPlanBackfillFailure] = Field(default_factory=list)
    has_more: bool
    next_after_id: str | None = None
    dry_run: bool = False


class CheckPlanRegenerateRequest(StrictModel):
    expected_revision: int = Field(ge=0)
    dry_run: bool = True


class CheckPlanRegenerateResponse(StrictModel):
    restriction_id: str
    revision: int
    dry_run: bool
    plan: CheckPlan
    trace: dict[str, Any] | None = None


class CheckPlanReplanRequest(StrictModel):
    """One resumable page of plans built by an older planner version.

    ``dry_run`` (the default) plans without writing, so the transition summary can be
    reviewed before the page is applied.
    """

    limit: int = Field(default=50, ge=1, le=500)
    after_id: str | None = Field(default=None, min_length=1, max_length=128)
    dry_run: bool = True
    include_items: bool = True


class CheckPlanReplanItem(StrictModel):
    restriction_id: str
    before_template: str | None = None
    before_status: str | None = None
    after_template: str | None = None
    after_status: str | None = None
    blocked_reasons: list[str] = Field(default_factory=list)
    written: bool = False
    error: str | None = None


class CheckPlanReplanResponse(StrictModel):
    planner_version: int
    selected: int
    written: int
    failed: int
    # "auto->unsupported", "unsupported->auto", "auto->auto", ...
    transitions: dict[str, int] = Field(default_factory=dict)
    templates: dict[str, int] = Field(default_factory=dict)
    blocked_reasons: dict[str, int] = Field(default_factory=dict)
    items: list[CheckPlanReplanItem] = Field(default_factory=list)
    has_more: bool
    next_after_id: str | None = None
    dry_run: bool = True


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


class ObjectAttributeThresholdParams(StrictModel):
    """Every object of a layer compares one numeric attribute with a constant."""

    objects_layer: str = Field(
        min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$"
    )
    attribute_role: str = Field(
        min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$"
    )
    operator: Literal["<", "<=", ">", ">=", "=="]
    threshold: float = Field(ge=-1_000_000_000, le=1_000_000_000)
    unit: str = Field(min_length=1, max_length=32)
    result_mode: Literal["violated", "passed", "both"] = "both"


class TimeLimit(StrictModel):
    kind: Literal["time"]
    minutes: float = Field(gt=0, le=240)


class DistanceLimit(StrictModel):
    kind: Literal["distance"]
    meters: float = Field(gt=0, le=100_000)


class AccessibilityWithinParams(StrictModel):
    """Every object must reach a neighbour within a time or route length.

    ``buffer_v1`` approximates the route by a straight-line radius
    ``(minutes * speed_m_per_min | meters) / detour_factor``; a street-graph
    measurement will be a separate ``measurement`` value. ``mode="transport"`` is a
    transport accessibility estimated with an average transport speed: a rough
    approximation without a road graph.
    """

    objects_layer: str = Field(
        min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$"
    )
    required_neighbor_layers: list[RoleName] = Field(min_length=1, max_length=16)
    limit: Annotated[TimeLimit | DistanceLimit, Field(discriminator="kind")]
    speed_m_per_min: float = Field(default=80.0, gt=0, le=1000)
    detour_factor: float = Field(default=1.3, ge=1, le=3)
    measurement: Literal["buffer_v1"] = "buffer_v1"
    mode: Literal["walk", "transport"] = "walk"
    minimum_neighbors: int = Field(default=1, ge=1, le=1000)
    result_mode: Literal["violated", "passed", "both"] = "both"


class ServiceProvisionParams(StrictModel):
    """Residents' demand for a service type must be met within its accessibility.

    ``capacity_per_1000`` and ``accessibility`` come from the norm; ``None`` keeps the
    Urban API normative of the service type. A norm of the "1 object per N residents"
    kind sets ``residents_per_service`` instead of ``capacity_per_1000``: every
    resident is then demand and each object serves N of them. A building is violated
    when the share of its demand served within accessibility is below ``min_provision``.
    """

    services_layer: str = Field(
        min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$"
    )
    capacity_per_1000: float | None = Field(default=None, gt=0, le=100_000)
    residents_per_service: float | None = Field(default=None, gt=0, le=10_000_000)
    accessibility: (
        Annotated[TimeLimit | DistanceLimit, Field(discriminator="kind")] | None
    ) = None
    # "transport": the norm's transport accessibility, given as the straight-line
    # distance an average transport covers in that time (a rough approximation).
    accessibility_mode: Literal["walk", "transport"] = "walk"
    min_provision: float = Field(default=1.0, gt=0, le=1)
    result_mode: Literal["violated", "passed", "both"] = "both"

    @model_validator(mode="after")
    def one_capacity_basis(self) -> "ServiceProvisionParams":
        if (
            self.capacity_per_1000 is not None
            and self.residents_per_service is not None
        ):
            raise ValueError(
                "capacity_per_1000 and residents_per_service exclude each other"
            )
        return self


PARAM_MODELS: dict[str, type[BaseModel]] = {
    "distance_from_source": DistanceFromSourceParams,
    "distance_table": DistanceTableParams,
    "presence_within": PresenceWithinParams,
    "zonal_attribute_threshold": ZonalAttributeThresholdParams,
    "zonal_ratio": ZonalRatioParams,
    "object_attribute_threshold": ObjectAttributeThresholdParams,
    "accessibility_within": AccessibilityWithinParams,
    "service_provision": ServiceProvisionParams,
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
    elif isinstance(params, ObjectAttributeThresholdParams):
        used_layers = {params.objects_layer}
        used_attributes = {params.attribute_role}
    elif isinstance(params, AccessibilityWithinParams):
        used_layers = {params.objects_layer, *params.required_neighbor_layers}
    elif isinstance(params, ServiceProvisionParams):
        used_layers = {params.services_layer}
    else:  # pragma: no cover - PARAM_MODELS is the closed v1 manifest
        raise ValueError(f"unsupported params model: {type(params).__name__}")

    if plan.scope is not None:
        used_layers.add(plan.scope.layer)
        used_attributes.add(plan.scope.attribute)
        scope_on = {item.role: item.on for item in requirements.attributes}
        if plan.scope.attribute in scope_on and (
            scope_on[plan.scope.attribute] != plan.scope.layer
        ):
            raise ValueError("scope attribute is declared on another layer")
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
    planner_version: int | None = None
    # Planner passes behind an automatic revision (deterministic, rewrite votes, verifier).
    trace: dict[str, Any] | None = None
