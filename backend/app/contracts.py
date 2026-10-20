"""ADC 原型的数据契约；只检查数据结构，不替代原文审核和规则核验。
定义公共校验规则
定义 ADC 需求快照
定义证据和参数事实
定义约束判定结果
"""

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    model_validator,
)

# 字符串必须真实为 str，去掉首尾空白后不能为空。
Text = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1),
]

# strict=True 避免把 True 当成通道数 1。
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
PositiveNumber = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]

ParameterName = Literal[
    "supply_voltage",
    "interface",
    "resolution_bits",
    "input_channels",
    "sample_rate",
]


def unique_ids(values: list[str]) -> list[str]:
    """检查引用列表。

    输入：已经清理过的 ID 列表。
    输出：没有重复引用的原列表。
    边界：重复引用直接报错，避免静默修改调用方的数据。
    """
    if len(values) != len(set(values)):
        raise ValueError("引用 ID 不能重复")
    return values


IdList = Annotated[list[Text], AfterValidator(unique_ids)]


class Contract(BaseModel):
    """公共配置：禁止未知字段、拒绝非有限数值、字段赋值时重新校验。"""

    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        validate_assignment=True,
    )


class AdcRequirements(Contract):
    """保存需求快照；None 表示未明确，不能推断为没有限制或已经满足。"""

    id: Text
    candidate_parts: Annotated[IdList, Field(min_length=1)]

    supply_voltage_v: PositiveNumber
    interface: Literal["I2C", "SPI"]
    min_resolution_bits: PositiveInt
    required_channels: PositiveInt

    # 每个通道的有效结果速率要求，不能直接与器件总转换速率混用。
    min_samples_per_channel_sps: PositiveNumber | None = None

    input_mode: Literal["single_ended", "differential"] | None = None
    input_min_v: Decimal | None = None
    input_max_v: Decimal | None = None
    simultaneous_sampling: bool | None = Field(default=None, strict=True)

    # 封装偏好与硬性要求分开保存。
    preferred_packages: IdList = Field(default_factory=list)

    @model_validator(mode="after")
    def check_input_range(self) -> Self:
        """检查需求中的输入范围。

        输入：当前需求对象。
        输出：检查通过的自身。
        边界：允许只知道一侧；两侧已知时，下限不能超过上限。
        """
        if (
            self.input_min_v is not None
            and self.input_max_v is not None
            and self.input_min_v > self.input_max_v
        ):
            raise ValueError("输入电压下限不能大于上限")
        return self


class Evidence(Contract):
    """保存原文及文档版本定位；PDF 页序从 1 开始。"""

    id: Text
    document_id: Text

    # 版本标识与文件哈希一起保存，不能只依赖可能更新的下载网址。
    document_version: Text
    document_sha256: Annotated[
        str,
        StringConstraints(pattern=r"^[0-9a-f]{64}$"),
    ]
    source_url: HttpUrl

    pdf_page: PositiveInt
    printed_page: Text | None = None
    section: Text | None = None
    quote: Text


class NumericValue(Contract):
    """保存数值界限；两侧相等表示单值，None 表示该侧未给出。"""

    kind: Literal["numeric"] = "numeric"
    lower: Decimal | None = None
    upper: Decimal | None = None
    unit: Literal["V", "bit", "channel", "SPS"]

    @model_validator(mode="after")
    def check_bounds(self) -> Self:
        """检查参数上下限。

        输入：当前数值对象。
        输出：检查通过的自身。
        边界：至少提供一侧；缺失一侧不能被当作无限范围。
        """
        if self.lower is None and self.upper is None:
            raise ValueError("至少提供一个数值界限")

        if self.lower is not None and self.upper is not None and self.lower > self.upper:
            raise ValueError("参数下限不能大于上限")

        return self


class TextValue(Contract):
    """保存枚举参数；当前用于规范化后的接口名称。"""

    kind: Literal["text"] = "text"
    value: Literal["I2C", "SPI"]


class ParameterFact(Contract):
    """保存参数声明及适用条件；提取出的声明不等于已经审核的事实。"""

    id: Text
    part_number: Text
    parameter: ParameterName

    # 根据 kind 区分数值和文本，避免接口名与数值混在一个字符串里。
    value: Annotated[
        NumericValue | TextValue,
        Field(discriminator="kind"),
    ]

    specification: Literal[
        "specified",
        "guaranteed",
        "typical",
        "absolute_maximum",
        "unknown",
    ] = "unknown"

    # 保存完整条件原文或说明；尚未查明时应明确写明。
    conditions: Text

    input_mode: Literal[
        "single_ended",
        "differential",
        "unknown",
    ] = "unknown"

    sampling_scope: Literal["total", "per_channel", "unknown"] = "unknown"
    active_channels: PositiveInt | None = None
    package: Text | None = None

    evidence_ids: Annotated[IdList, Field(min_length=1)]
    reviewed: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def check_parameter_type(self) -> Self:
        """检查参数类型、单位和基本数值。

        输入：当前参数声明。
        输出：检查通过的自身。
        边界：不判断原文真假、证据适用性或该参数是否满足用户需求。
        """
        if self.parameter == "interface":
            if not isinstance(self.value, TextValue):
                raise ValueError("接口参数必须使用 text 类型")
        else:
            expected_units = {
                "supply_voltage": "V",
                "resolution_bits": "bit",
                "input_channels": "channel",
                "sample_rate": "SPS",
            }

            if (
                not isinstance(self.value, NumericValue)
                or self.value.unit != expected_units[self.parameter]
            ):
                raise ValueError("数值参数类型或单位不匹配")

            bounds = (self.value.lower, self.value.upper)

            if any(bound is not None and bound <= 0 for bound in bounds):
                raise ValueError("当前五类参数的数值必须大于零")

            if self.parameter in {"resolution_bits", "input_channels"}:
                if any(
                    bound is not None and bound != bound.to_integral_value() for bound in bounds
                ):
                    raise ValueError("分辨率位数与通道数必须为整数")

        if self.parameter != "sample_rate" and (
            self.sampling_scope != "unknown" or self.active_channels is not None
        ):
            raise ValueError("采样率口径与活跃通道数仅用于 sample_rate 参数")

        return self


class ConstraintResult(Contract):
    """保存某候选的一项判定；跨对象引用由后续核验服务检查。"""

    id: Text
    requirement_id: Text
    part_number: Text
    parameter: ParameterName

    status: Literal[
        "satisfied",
        "not_satisfied",
        "unknown",
        "conflict",
    ]
    reason: Text

    fact_ids: IdList = Field(default_factory=list)
    evidence_ids: IdList = Field(default_factory=list)
    unresolved: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_support(self) -> Self:
        """检查判定是否附带必要依据。

        输入：当前判定对象。
        输出：检查通过的自身。
        边界：这里只检查引用数量和缺失说明，不保证引用内容正确。
        """
        if self.status != "unknown" and (not self.fact_ids or not self.evidence_ids):
            raise ValueError("明确判定和冲突判定必须引用参数声明与原文证据")

        if self.status == "unknown" and not self.unresolved:
            raise ValueError("待确认必须说明未解决的问题")

        if self.status == "conflict" and len(self.fact_ids) < 2:
            raise ValueError("冲突判定至少引用两条参数声明")

        return self
