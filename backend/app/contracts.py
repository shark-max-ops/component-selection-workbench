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
    """
    作用：检查引用列表是否存在重复 ID。
    输入：values - 已清理的引用 ID 列表。
    输出：list[str]，通过检查的原列表。
    """
    # 重复引用直接报错，避免静默修改调用方的数据。
    if len(values) != len(set(values)):
        raise ValueError("引用 ID 不能重复")
    return values


IdList = Annotated[list[Text], AfterValidator(unique_ids)]


class Contract(BaseModel):
    """
    作用：统一数据契约的字段校验配置。
    输入：子类声明的字段。
    输出：禁止未知字段并在赋值时校验的契约对象。
    """

    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        validate_assignment=True,
    )


class AdcRequirements(Contract):
    """
    作用：保存 ADC 选型需求快照。
    输入：需求 ID；候选型号；供电、接口、通道、分辨率及可选约束。
    输出：AdcRequirements 需求对象。
    """

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
        """
        作用：校验需求中的输入电压范围。
        输入：self - 当前需求对象。
        输出：Self，通过检查的当前对象。
        """
        # 允许只知道一侧；两侧已知时，下限不能超过上限。
        if (
            self.input_min_v is not None
            and self.input_max_v is not None
            and self.input_min_v > self.input_max_v
        ):
            raise ValueError("输入电压下限不能大于上限")
        return self


class Evidence(Contract):
    """
    作用：保存原文证据及文档版本定位。
    输入：证据 ID；文档 ID、版本及哈希；来源网址；页码；章节及原文。
    输出：Evidence 证据对象。
    """

    id: Text
    document_id: Text

    # 版本标识与文件哈希一起保存，不能只依赖可能更新的下载网址。
    document_version: Text
    document_sha256: Annotated[
        str,
        StringConstraints(pattern=r"^[0-9a-f]{64}$"),
    ]
    source_url: HttpUrl

    # PDF 页序从 1 开始，与解析器的零起始页索引区分。
    pdf_page: PositiveInt
    printed_page: Text | None = None
    section: Text | None = None
    quote: Text


class NumericValue(Contract):
    """
    作用：保存带单位的数值上下限。
    输入：kind - 数值类型标记；lower - 下限；upper - 上限；unit - 单位。
    输出：NumericValue 数值对象。
    """

    kind: Literal["numeric"] = "numeric"
    lower: Decimal | None = None
    upper: Decimal | None = None
    unit: Literal["V", "bit", "channel", "SPS"]

    @model_validator(mode="after")
    def check_bounds(self) -> Self:
        """
        作用：校验数值上下限是否完整且有序。
        输入：self - 当前数值对象。
        输出：Self，通过检查的当前对象。
        """
        # 至少提供一侧；缺失一侧不能被当作无限范围。
        if self.lower is None and self.upper is None:
            raise ValueError("至少提供一个数值界限")

        if self.lower is not None and self.upper is not None and self.lower > self.upper:
            raise ValueError("参数下限不能大于上限")

        return self


class TextValue(Contract):
    """
    作用：保存规范化的接口枚举值。
    输入：kind - 文本类型标记；value - I2C 或 SPI。
    输出：TextValue 接口对象。
    """

    kind: Literal["text"] = "text"
    value: Literal["I2C", "SPI"]


class ParameterFact(Contract):
    """
    作用：保存参数声明、适用条件与证据引用。
    输入：参数 ID；型号；参数类别及数值；规格类别；条件；证据 ID；审核状态。
    输出：ParameterFact 参数声明对象。
    """

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
        """
        作用：校验参数类型、单位及适用字段。
        输入：self - 当前参数声明。
        输出：Self，通过检查的当前对象。
        """
        # 不判断原文真假、证据适用性或该参数是否满足用户需求。
        # 接口只接受枚举文本，其他参数按类别检查数值单位。
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

            # 检查数值为正，位数和通道数还必须为整数。
            bounds = (self.value.lower, self.value.upper)

            if any(bound is not None and bound <= 0 for bound in bounds):
                raise ValueError("当前五类参数的数值必须大于零")

            if self.parameter in {"resolution_bits", "input_channels"}:
                if any(
                    bound is not None and bound != bound.to_integral_value() for bound in bounds
                ):
                    raise ValueError("分辨率位数与通道数必须为整数")

        # 采样口径和活跃通道数仅能附在采样率参数上。
        if self.parameter != "sample_rate" and (
            self.sampling_scope != "unknown" or self.active_channels is not None
        ):
            raise ValueError("采样率口径与活跃通道数仅用于 sample_rate 参数")

        return self


class ConstraintResult(Contract):
    """
    作用：保存候选器件的单项核验结果。
    输入：结果 ID；需求 ID；型号；参数；判定、原因、引用及未解决项。
    输出：ConstraintResult 判定对象。
    """

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
        """
        作用：检查判定是否附有必要引用和缺失说明。
        输入：self - 当前判定对象。
        输出：Self，通过检查的当前对象。
        """
        # 这里只检查引用数量和缺失说明，不保证引用内容正确。
        # 明确判定与冲突判定必须具备事实和证据引用。
        if self.status != "unknown" and (not self.fact_ids or not self.evidence_ids):
            raise ValueError("明确判定和冲突判定必须引用参数声明与原文证据")

        # 待确认需解释缺失项，冲突需引用至少两条参数声明。
        if self.status == "unknown" and not self.unresolved:
            raise ValueError("待确认必须说明未解决的问题")

        if self.status == "conflict" and len(self.fact_ids) < 2:
            raise ValueError("冲突判定至少引用两条参数声明")

        return self
