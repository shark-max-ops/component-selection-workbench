---
name: code-comment
description: 统一代码注释规范：类/函数用三引号文档字符串，逐块逻辑用 # 单行注释。Use when writing or modifying classes, functions, or code blocks and needing consistent comment style.
---

# Code Comment

统一代码注释规范：类 / 函数用三引号文档字符串（作用、输入、输出），每小块逻辑用 `#` 单行注释。

## 何时使用

- 新增或修改 `class` / `def` 时
- 编写或重构一段逻辑时

## 规则

### 1. 类注释

在 `class` 行下方紧跟文档字符串，固定三段：作用 / 输入 / 输出。

```python
class ToolRunner:
    """
    作用：执行工具调用并精简记录结果
    输入：无
    输出：无
    """
```

### 2. 函数注释

在 `def` 行下方紧跟文档字符串，固定三段：作用 / 输入 / 输出。

```python
def build_detail(request):
    """
    作用：从请求中提取工具调用的简要信息
    输入：request - 包含 tool_call 的请求对象
    输出：dict，含 tool、call_id、path 三个字段
    """
```

### 3. 代码块注释

每完成一小块逻辑，在其上方加一行 `#` 注释，说清这一段在干什么。

```python
# 只展示名称、调用标识和文件位置，不记录完整工具参数。
call = request.tool_call
detail = dict(tool=call["name"], call_id=call["id"])
path = call["args"].get("file_path")
```

## 完整示例

```python
class ToolRunner:
    """
    作用：执行工具调用并精简记录结果
    输入：无
    输出：无
    """

    def build_detail(self, request):
        """
        作用：从请求中提取工具调用的简要信息
        输入：request - 包含 tool_call 的请求对象
        输出：dict，含 tool、call_id、path 三个字段
        """
        # 只展示名称、调用标识和文件位置，不记录完整工具参数。
        call = request.tool_call
        detail = dict(tool=call["name"], call_id=call["id"])
        path = call["args"].get("file_path")

        # 路径缺失时留空，避免下游判空报错。
        detail["path"] = path or ""

        return detail
```

## 最佳实践

- 注释只写“做什么”，一句话说清，不写实现细节的废话
- 无输入写“无”，无返回写“无”；多参数用 `；` 分隔
- 不为显而易见的代码加注释
- 统一中文、统一标点

