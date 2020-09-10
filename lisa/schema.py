from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Type, TypeVar, Union, cast

from dataclasses_json import (  # type: ignore
    CatchAll,
    DataClassJsonMixin,
    LetterCase,
    Undefined,
    config,
    dataclass_json,
)
from marshmallow import ValidationError, fields, validate

from lisa import search_space
from lisa.secret import PATTERN_HEADTAIL, add_secret
from lisa.util import LisaException, constants

"""
Schema is dealt with three components,
1. dataclasses. It's a builtin class, uses to define schema of an instance. field()
   function uses to describe a field.
2. dataclasses_json. Serializer. config() function customizes this component.
3. marshmallow. Validator. It's wrapped by dataclasses_json. config(mm_field=xxx)
   function customizes this component.
"""


def metadata(
    field_function: Optional[Callable[..., Any]] = None, *args: Any, **kwargs: Any
) -> Any:
    """
    wrap for shorter
    """
    if field_function is None:
        field_function = fields.Raw
    assert field_function
    encoder = kwargs.pop("encoder", None)
    decoder = kwargs.pop("decoder", None)
    # keep data_key for underlying marshmallow
    field_name = kwargs.get("data_key")
    return config(
        field_name=field_name,
        encoder=encoder,
        decoder=decoder,
        mm_field=field_function(*args, **kwargs),
    )


T_REQUIREMENT = TypeVar("T_REQUIREMENT", bound=search_space.RequirementMixin)
T = TypeVar("T", bound=DataClassJsonMixin)
U = TypeVar("U")


class ListableValidator(validate.Validator):
    default_message = ""

    def __init__(
        self,
        value_type: U,
        value_validator: Optional[
            Union[validate.Validator, List[validate.Validator]]
        ] = None,
        error: str = "",
    ) -> None:
        self._value_type: Any = value_type
        if value_validator is None:
            self._inner_validator: List[validate.Validator] = []
        elif callable(value_validator):
            self._inner_validator = [value_validator]
        elif isinstance(value_validator, list):
            self._inner_validator = list(value_validator)
        else:
            raise ValueError(
                "The 'value_validator' parameter must be a callable "
                "or a collection of callables."
            )
        self.error: str = error or self.default_message

    def _repr_args(self) -> str:
        return f"_inner_validator={self._inner_validator}"

    def _format_error(self, value: Any) -> str:
        return self.error.format(input=value)

    def __call__(self, value: Any) -> Any:
        if isinstance(value, self._value_type):
            if self._inner_validator:
                for validator in self._inner_validator:
                    validator(value)  # type: ignore
        elif isinstance(value, list):
            for value_item in value:
                assert isinstance(value_item, self._value_type), (
                    f"must be '{self._value_type}' but '{value_item}' "
                    f"is '{type(value_item)}'"
                )
                if self._inner_validator:
                    for validator in self._inner_validator:
                        validator(value_item)  # type: ignore
        elif value is not None:
            raise ValidationError(
                f"must be Union[{self._value_type}, List[{self._value_type}]], "
                f"but '{value}' is '{type(value)}'"
            )
        return value


@dataclass_json(letter_case=LetterCase.CAMEL, undefined=Undefined.INCLUDE)
@dataclass
class ExtendableSchemaMixin:
    extend_schemas: CatchAll = None

    def get_extended_runbook(
        self, runbook_type: Type[T], field_name: str = ""
    ) -> Optional[T]:
        """
        runbook_type: type of runbook
        field_name: the field name which stores the data, if it's "", get it from type
        """
        if not hasattr(self, "__extended_runbook"):
            assert issubclass(
                runbook_type, DataClassJsonMixin
            ), "runbook_type must annotate from DataClassJsonMixin"
            if not field_name:
                assert hasattr(self, constants.TYPE), (
                    f"cannot find type attr on '{runbook_type.__name__}'."
                    f"either set field_name or make sure type attr exists."
                )
                field_name = getattr(self, constants.TYPE)

            if self.extend_schemas and field_name in self.extend_schemas:
                self.__extended_runbook: Optional[
                    T
                ] = runbook_type.schema().load(  # type:ignore
                    self.extend_schemas[field_name]
                )
            else:
                self.__extended_runbook = None

        return self.__extended_runbook


@dataclass_json(letter_case=LetterCase.CAMEL)
@dataclass
class Strategy:
    """
    for simple merge, this part is optional.
    operations include:
    overwrite: default behavior. add non-exist items and replace exist.
    remove: remove specified path totally.
    add: add non-exist, not replace exist.
    """

    path: str = field(default="", metadata=metadata(required=True))
    operation: str = field(
        default=constants.OPERATION_OVERWRITE,
        metadata=metadata(
            required=True,
            validate=validate.OneOf(
                [
                    constants.OPERATION_ADD,
                    constants.OPERATION_OVERWRITE,
                    constants.OPERATION_REMOVE,
                ]
            ),
        ),
    )


@dataclass_json(letter_case=LetterCase.CAMEL)
@dataclass
class Parent:
    """
    share runbook for similar runs.
    """

    path: str = field(default="", metadata=metadata(required=True))
    strategy: List[Strategy] = field(
        default_factory=list, metadata=metadata(required=True),
    )


@dataclass_json(letter_case=LetterCase.CAMEL)
@dataclass
class Extension:
    """
    add extended classes can be put in folders and include here. it doesn't matter how
    those files are organized, lisa loads by their inherits relationship. if there is
    any conflict on type name, there should be an error message.
    """

    paths: List[str] = field(default_factory=list, metadata=metadata(required=True))


@dataclass_json(letter_case=LetterCase.CAMEL)
@dataclass
class VariableEntry:
    is_secret: bool = False
    mask: str = ""
    value: Union[str, bool, int] = ""


@dataclass_json(letter_case=LetterCase.CAMEL)
@dataclass
class Variable:
    """
    it uses to support variables in other fields.
    duplicate items will be overwritten one by one.
    if a variable is not defined here, LISA can fail earlier to ask check it.
    file path is relative to LISA command starts.
    """

    # If it's secret, it will be removed from log and other output information.
    # secret files also need to be removed after test
    # it's not recommended highly to put secret in runbook directly.
    is_secret: bool = False

    # continue to support v2 format. it's simple.
    file: str = field(
        default="",
        metadata=metadata(validate=validate.Regexp(r"[\w\W]+[.](xml|yml|yaml)$")),
    )

    name: str = field(default="")
    value_raw: Union[str, bool, int, Dict[Any, Any]] = field(
        default="", metadata=metadata(data_key="value")
    )

    def __post_init__(self, *args: Any, **kwargs: Any) -> None:
        if self.file and (self.name or self.value_raw):
            raise LisaException(
                f"file cannot be specified with name or value"
                f"file: '{self.file}'"
                f"name: '{self.name}'"
                f"value: '{self.value_raw}'"
            )

        if isinstance(self.value_raw, dict):
            self.value: Union[str, bool, int, VariableEntry] = cast(
                VariableEntry,
                VariableEntry.schema().load(self.value_raw),  # type:ignore
            )
        else:
            self.value = self.value_raw


@dataclass_json(letter_case=LetterCase.CAMEL)
@dataclass
class ArtifactLocation:
    type: str = field(
        default="", metadata=metadata(required=True, validate=validate.OneOf([])),
    )
    path: str = field(default="", metadata=metadata(required=True))

    def __post_init__(self, *args: Any, **kwargs: Any) -> None:
        add_secret(self.path)


@dataclass_json(letter_case=LetterCase.CAMEL)
@dataclass
class Artifact:
    # name is optional. artifacts can be referred by name or index.
    name: str = ""
    type: str = field(
        default="", metadata=metadata(required=True, validate=validate.OneOf([])),
    )
    locations: List[ArtifactLocation] = field(default_factory=list)


@dataclass_json(letter_case=LetterCase.CAMEL)
@dataclass
class Notifier:
    """
    it sends test progress and results to any place wanted.
    detail types are defined in notifier itself, allowed items are handled in code.
    """

    type: str = field(
        default="", metadata=metadata(required=True, validate=validate.OneOf([])),
    )


FEATURE_NAME_RDMA = "RDMA"


@dataclass_json(letter_case=LetterCase.CAMEL)
@dataclass
class Feature:
    name: str = ""
    enabled: bool = True
    can_disable: bool = False


class Features:
    RDMA = Feature(name=FEATURE_NAME_RDMA)


@dataclass_json(letter_case=LetterCase.CAMEL, undefined=Undefined.INCLUDE)
@dataclass
class NodeSpace(search_space.RequirementMixin, ExtendableSchemaMixin):
    type: str = field(
        default=constants.ENVIRONMENTS_NODES_REQUIREMENT,
        metadata=metadata(
            required=True,
            validate=validate.OneOf([constants.ENVIRONMENTS_NODES_REQUIREMENT]),
        ),
    )
    name: str = ""
    is_default: bool = field(default=False)
    # optional, if there is only one artifact.
    artifact: str = field(default="")
    node_count: search_space.CountSpace = field(
        default=search_space.IntRange(min=1), metadata=metadata(data_key="nodeCount"),
    )
    core_count: search_space.CountSpace = field(
        default=search_space.IntRange(min=1),
        metadata=metadata(data_key="coreCount", validate=validate.Range(min=1)),
    )
    memory_mb: search_space.CountSpace = field(
        default=search_space.IntRange(min=512),
        metadata=metadata(data_key="memoryMb", validate=validate.Range(min=512)),
    )
    nic_count: search_space.CountSpace = field(
        default=search_space.IntRange(min=1),
        metadata=metadata(data_key="nicCount", validate=validate.Range(min=1)),
    )
    gpu_count: search_space.CountSpace = field(
        default=search_space.IntRange(min=0),
        metadata=metadata(data_key="gpuCount", validate=validate.Range(min=0)),
    )
    # all features on requirement should be included.
    # all features on capability can be included.
    features: Optional[search_space.SetSpace[Feature]] = field(
        default=None, metadata=metadata()
    )
    # set by requirements
    # capability's is ignored
    excluded_features: Optional[search_space.SetSpace[Feature]] = field(
        default=None, metadata=metadata(data_key="excludedFeatures")
    )

    def __post_init__(self, *args: Any, **kwargs: Any) -> None:
        if self.features:
            self.features.is_allow_set = True
        if self.excluded_features:
            self.excluded_features.is_allow_set = False

    def __eq__(self, other: object) -> bool:
        assert isinstance(other, NodeSpace), f"actual: {type(other)}"

        result = (
            self.type == other.type
            and self.name == other.name
            and self.is_default == other.is_default
            and self.artifact == other.artifact
            and self.node_count == other.node_count
            and self.core_count == other.core_count
            and self.memory_mb == other.memory_mb
            and self.nic_count == other.nic_count
            and self.gpu_count == other.gpu_count
            and self.features == other.features
            and self.excluded_features == other.excluded_features
        )
        return result

    def __repr__(self) -> str:
        return (
            f"type:{self.type}, name:{self.name}, "
            f"is_default:{self.is_default}, artifact:{self.artifact}, "
            f"count:{self.node_count}, core:{self.core_count}, "
            f"mem:{self.memory_mb}, nic:{self.nic_count}, gpu:{self.gpu_count}, "
            f"f:{self.features}, ef:{self.excluded_features}"
        )

    def check(self, capability: Any) -> search_space.ResultReason:
        result = search_space.ResultReason()
        if capability is None:
            result.add_reason("capability shouldn't be None")

        if self.features:
            assert self.features.is_allow_set, "features should be allow set"
        if self.excluded_features:
            assert (
                not self.excluded_features.is_allow_set
            ), "excluded_features shouldn't be allow set"

        assert isinstance(capability, NodeSpace), f"actual: {type(capability)}"

        must_included_capability: Optional[search_space.SetSpace[Feature]] = None

        if capability.features and self.excluded_features:
            must_included_capability = search_space.SetSpace[Feature]()
            for feature in capability.features:
                if feature.enabled and feature.can_disable is False:
                    must_included_capability.add(feature)

        if (
            not capability.node_count
            or not capability.core_count
            or not capability.memory_mb
            or not capability.nic_count
        ):
            result.add_reason(
                "node_count, core_count, memory_mb, nic_count shouldn't be None"
            )

        if isinstance(self.node_count, int) and isinstance(capability.node_count, int):
            if self.node_count > capability.node_count:
                result.add_reason(
                    f"capability node count {capability.node_count} "
                    f"must be more than requirement {self.node_count}"
                )
        else:
            result.merge(
                search_space.check_countspace(self.node_count, capability.node_count),
                "node_count",
            )

        result.merge(
            search_space.check_countspace(self.core_count, capability.core_count),
            "core_count",
        )
        result.merge(
            search_space.check_countspace(self.memory_mb, capability.memory_mb),
            "memory_mb",
        )
        result.merge(
            search_space.check_countspace(self.nic_count, capability.nic_count),
            "nic_count",
        )
        result.merge(
            search_space.check_countspace(self.gpu_count, capability.gpu_count),
            "gpu_count",
        )
        result.merge(
            search_space.check(self.features, capability.features), "features",
        )
        if self.excluded_features is not None:
            result.merge(
                search_space.check(self.excluded_features, must_included_capability),
                "excluded_features",
            )

        return result

    @classmethod
    def from_value(cls, value: Any) -> Any:
        assert isinstance(value, NodeSpace), f"actual: {type(value)}"
        node = NodeSpace()
        node.node_count = value.node_count
        node.core_count = value.core_count
        node.memory_mb = value.memory_mb
        node.nic_count = value.nic_count
        node.gpu_count = value.gpu_count

        if value.features:
            for feature in value.features:
                if feature.enabled or feature.can_disable:
                    if not node.features:
                        node.features = search_space.SetSpace[Feature]()
                    node.features.add(feature)
                else:
                    if not node.excluded_features:
                        node.excluded_features = search_space.SetSpace[Feature]()
                    node.excluded_features.add(feature)

        return node

    def _generate_min_capability(self, capability: Any) -> Any:
        min_value = NodeSpace()
        assert isinstance(capability, NodeSpace), f"actual: {type(capability)}"

        if self.node_count or capability.node_count:

            if isinstance(self.node_count, int) and isinstance(
                capability.node_count, int
            ):
                # capability can have more node
                min_value.node_count = capability.node_count
            else:
                min_value.node_count = search_space.generate_min_capability_countspace(
                    self.node_count, capability.node_count
                )
        else:
            raise LisaException("node_count cannot be zero")
        if self.core_count or capability.core_count:
            min_value.core_count = search_space.generate_min_capability_countspace(
                self.core_count, capability.core_count
            )
        else:
            raise LisaException("core_count cannot be zero")
        if self.memory_mb or capability.memory_mb:
            min_value.memory_mb = search_space.generate_min_capability_countspace(
                self.memory_mb, capability.memory_mb
            )
        else:
            raise LisaException("memory_mb cannot be zero")
        if self.nic_count or capability.nic_count:
            min_value.nic_count = search_space.generate_min_capability_countspace(
                self.nic_count, capability.nic_count
            )
        else:
            raise LisaException("nic_count cannot be zero")
        if self.gpu_count or capability.gpu_count:
            min_value.gpu_count = search_space.generate_min_capability_countspace(
                self.gpu_count, capability.gpu_count
            )
        else:
            min_value.gpu_count = 0

        min_value.features = search_space.SetSpace[Feature]()
        if self.features:
            min_value.features.update(self.features)
        if self.excluded_features:
            for excluded_feature in self.excluded_features:
                excluded_feature.enabled = False
                min_value.features.add(excluded_feature)
        return min_value


@dataclass_json(letter_case=LetterCase.CAMEL)
@dataclass
class LocalNode:
    type: str = field(
        default=constants.ENVIRONMENTS_NODES_LOCAL,
        metadata=metadata(
            required=True,
            validate=validate.OneOf([constants.ENVIRONMENTS_NODES_LOCAL]),
        ),
    )
    name: str = ""
    is_default: bool = field(default=False)
    capability: NodeSpace = field(default=NodeSpace(node_count=1))


@dataclass_json(letter_case=LetterCase.CAMEL)
@dataclass
class RemoteNode:
    type: str = field(
        default=constants.ENVIRONMENTS_NODES_REMOTE,
        metadata=metadata(
            required=True,
            validate=validate.OneOf([constants.ENVIRONMENTS_NODES_REMOTE]),
        ),
    )
    name: str = ""
    is_default: bool = field(default=False)
    address: str = ""
    port: int = field(
        default=22, metadata=metadata(validate=validate.Range(min=1, max=65535))
    )
    public_address: str = ""
    public_port: int = field(
        default=22,
        metadata=metadata(
            data_key="publicPort", validate=validate.Range(min=1, max=65535)
        ),
    )
    username: str = field(default="", metadata=metadata(required=True))
    password: str = ""
    private_key_file: str = ""
    capability: NodeSpace = field(default=NodeSpace(node_count=1))

    def __post_init__(self, *args: Any, **kwargs: Any) -> None:
        add_secret(self.address)
        add_secret(self.public_address)
        add_secret(self.username, PATTERN_HEADTAIL)
        add_secret(self.password)
        add_secret(self.private_key_file)

        if not self.address and not self.public_address:
            raise LisaException(
                "at least one of address and publicAddress need to be set"
            )
        elif not self.address:
            self.address = self.public_address
        elif not self.public_address:
            self.public_address = self.address

        if not self.port and not self.public_port:
            raise LisaException("at least one of port and publicPort need to be set")
        elif not self.port:
            self.port = self.public_port
        elif not self.public_port:
            self.public_port = self.port

        if not self.password and not self.private_key_file:
            raise LisaException(
                "at least one of password and privateKeyFile need to be set"
            )


@dataclass_json(letter_case=LetterCase.CAMEL)
@dataclass
class EnvironmentSpace(search_space.RequirementMixin):
    topology: str = field(
        default=constants.ENVIRONMENTS_SUBNET,
        metadata=metadata(validate=validate.OneOf([constants.ENVIRONMENTS_SUBNET])),
    )
    nodes: List[NodeSpace] = field(default_factory=list)

    def __eq__(self, other: Any) -> bool:
        assert isinstance(other, EnvironmentSpace), f"actual: {type(other)}"

        # ignore name on comparison, so env can be merged.
        result = self.topology == other.topology and search_space.equal_list(
            self.nodes, other.nodes
        )
        return result

    def check(self, capability: Any) -> search_space.ResultReason:
        assert isinstance(capability, EnvironmentSpace), f"actual: {type(capability)}"
        result = search_space.ResultReason()
        if not capability.nodes:
            result.add_reason("nodes shouldn't be None or empty")
        else:
            if self.nodes:
                for index, current_req in enumerate(self.nodes):
                    if len(capability.nodes) == 1:
                        current_cap = capability.nodes[0]
                    else:
                        current_cap = capability.nodes[index]
                    result.merge(
                        search_space.check(current_req, current_cap), str(index),
                    )
                    if not result.result:
                        break

        return result

    @classmethod
    def from_value(cls, value: Any) -> Any:
        assert isinstance(value, EnvironmentSpace), f"actual: {type(value)}"
        env = EnvironmentSpace()
        env.nodes = value.nodes
        if value.nodes:
            env.nodes = list()
            for value_capability in value.nodes:
                env.nodes.append(NodeSpace.from_value(value_capability))

        return env

    def _generate_min_capability(self, capability: Any) -> Any:
        env = EnvironmentSpace(topology=self.topology)
        assert isinstance(capability, EnvironmentSpace), f"actual: {type(capability)}"
        assert capability.nodes
        for index, current_req in enumerate(self.nodes):
            if len(capability.nodes) == 1:
                current_cap = capability.nodes[0]
            else:
                current_cap = capability.nodes[index]

            env.nodes.append(current_req.generate_min_capability(current_cap))

        return env


@dataclass_json(letter_case=LetterCase.CAMEL)
@dataclass
class Environment:
    name: str = field(default="")
    topology: str = field(
        default=constants.ENVIRONMENTS_SUBNET,
        metadata=metadata(validate=validate.OneOf([constants.ENVIRONMENTS_SUBNET])),
    )
    nodes_raw: Optional[List[Any]] = field(
        default=None, metadata=metadata(data_key=constants.NODES),
    )
    nodes_requirement: Optional[List[NodeSpace]] = None
    capability: EnvironmentSpace = field(default_factory=EnvironmentSpace)

    def __post_init__(self, *args: Any, **kwargs: Any) -> None:
        self.nodes: Optional[List[Union[LocalNode, RemoteNode]]] = None
        if self.nodes_raw is not None:
            self.nodes = []
            self.capability.topology = self.topology
            for node_raw in self.nodes_raw:
                node_type = node_raw[constants.TYPE]
                if node_type == constants.ENVIRONMENTS_NODES_LOCAL:
                    node: Union[
                        LocalNode, RemoteNode
                    ] = LocalNode.schema().load(  # type:ignore
                        node_raw
                    )
                    if self.nodes is None:
                        self.nodes = []
                    self.nodes.append(node)
                    self.capability.nodes.append(node.capability)
                elif node_type == constants.ENVIRONMENTS_NODES_REMOTE:
                    node = RemoteNode.schema().load(node_raw)  # type:ignore
                    if self.nodes is None:
                        self.nodes = []
                    self.nodes.append(node)
                    self.capability.nodes.append(node.capability)
                elif node_type == constants.ENVIRONMENTS_NODES_REQUIREMENT:
                    node_requirement = NodeSpace.schema().load(node_raw)  # type:ignore
                    if self.nodes_requirement is None:
                        self.nodes_requirement = []
                    self.nodes_requirement.append(node_requirement)
                    self.capability.nodes.append(node_requirement)
                else:
                    raise LisaException(f"unknown node type '{node_type}': {node_raw}")
            self.nodes_raw = None


@dataclass_json(letter_case=LetterCase.CAMEL)
@dataclass
class EnvironmentRoot:
    max_concurrency: int = field(
        default=1,
        metadata=metadata(data_key="maxConcurrency", validate=validate.Range(min=1)),
    )
    allow_create: bool = True
    warn_as_error: bool = field(default=False)
    environments: List[Environment] = field(default_factory=list)


@dataclass_json(letter_case=LetterCase.CAMEL, undefined=Undefined.INCLUDE)
@dataclass
class Platform(ExtendableSchemaMixin):
    type: str = field(
        default=constants.PLATFORM_READY, metadata=metadata(required=True),
    )

    admin_username: str = "lisatest"
    admin_password: str = ""
    admin_private_key_file: str = ""

    # True means not to delete an environment, even it's created by lisa
    reserve_environment: bool = False

    def __post_init__(self, *args: Any, **kwargs: Any) -> None:
        add_secret(self.admin_username, PATTERN_HEADTAIL)
        add_secret(self.admin_password)
        add_secret(self.admin_private_key_file)

        if self.type != constants.PLATFORM_READY:
            if self.admin_password and self.admin_private_key_file:
                raise LisaException(
                    "only one of admin_password and admin_private_key_file can be set"
                )
            elif not self.admin_password and not self.admin_private_key_file:
                raise LisaException(
                    "one of admin_password and admin_private_key_file must be set"
                )


@dataclass_json(letter_case=LetterCase.CAMEL)
@dataclass
class Criteria:
    """
    all rules in same criteria are AND condition.
    we may support richer conditions later.
    match case by name pattern
    """

    name: Optional[str] = None
    area: Optional[str] = None
    category: Optional[str] = None
    # the runbook is complex to convert, so manual overwrite it in __post_init__.
    priority: Optional[Union[int, List[int]]] = field(
        default=None,
        metadata=metadata(
            validate=ListableValidator(int, validate.Range(min=0, max=3))
        ),
    )
    # tags is a simple way to include test cases within same topic.
    tags: Optional[Union[str, List[str]]] = field(
        default=None, metadata=metadata(validate=ListableValidator(str))
    )


@dataclass_json(letter_case=LetterCase.CAMEL)
@dataclass
class TestCase:
    """
    rules apply ordered on previous selection.
    The order of test cases running is not guaranteed, until it set dependencies.
    """

    name: str = ""
    criteria: Optional[Criteria] = None
    # specify use this rule to select or drop test cases. if it's forced include or
    # exclude, it won't be effect by following select actions. And it fails if
    # there are force rules conflict.
    select_action: str = field(
        default=constants.TESTCASE_SELECT_ACTION_INCLUDE,
        metadata=config(
            mm_field=fields.String(
                validate=validate.OneOf(
                    [
                        # none means this action part doesn't include or exclude cases
                        constants.TESTCASE_SELECT_ACTION_NONE,
                        constants.TESTCASE_SELECT_ACTION_INCLUDE,
                        constants.TESTCASE_SELECT_ACTION_FORCE_INCLUDE,
                        constants.TESTCASE_SELECT_ACTION_EXCLUDE,
                        constants.TESTCASE_SELECT_ACTION_FORCE_EXCLUDE,
                    ]
                )
            ),
        ),
    )
    # if it's false, the test cases are disable in current run.
    # it uses to control test cases dynamic form command line.
    enable: bool = field(default=True)
    # run this group of test cases several times
    # default is 1
    times: int = field(default=1, metadata=metadata(validate=validate.Range(min=1)))
    # retry times if fails. Default is 0, not to retry.
    retry: int = field(default=0, metadata=metadata(validate=validate.Range(min=0)))
    # each case with this rule will be run in a new environment.
    use_new_environment: bool = False
    # Once it's set, failed test result will be rewrite to success
    # it uses to work around some cases temporarily, don't overuse it.
    # default is false
    ignore_failure: bool = False
    # case should run on a specified environment
    environment: str = ""


@dataclass_json(letter_case=LetterCase.CAMEL)
@dataclass
class Runbook:
    # run name prefix to help grouping results and put it in title.
    name: str = "not_named"
    parent: Optional[List[Parent]] = field(default=None)
    extension: Optional[Extension] = field(default=None)
    variable: Optional[List[Variable]] = field(default=None)
    artifact: Optional[List[Artifact]] = field(default=None)
    environment: Optional[EnvironmentRoot] = field(default=None)
    notifier: Optional[List[Notifier]] = field(default=None)
    platform: List[Platform] = field(default_factory=list)
    testcase: List[TestCase] = field(default_factory=list)

    def __post_init__(self, *args: Any, **kwargs: Any) -> None:
        if not self.platform:
            self.platform = [Platform(type=constants.PLATFORM_READY)]

        if not self.testcase:
            self.testcase = [TestCase(name="test", criteria=Criteria(area="demo"))]