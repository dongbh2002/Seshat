"""Runtime 集成测试，验证默认装配下不触发工具调用的真实多轮模型交互。"""

from backend.bootstrap import Identity, create_default_runtime
from backend.config import load_settings


def test_runtime_multi_turn_chat_without_tools() -> None:
    """用默认装配执行两轮不需要工具的真实对话，并验证 Runtime 保留了上一轮消息。

    Returns:
        None；模型未返回有效结果或未保留上下文时由断言报告失败。
    """
    runtime = create_default_runtime(
        load_settings(),
        Identity(tenant_id="233", user_id="dbh"),
    )

    first_reply = runtime.run("请记住数字 233，并只回复“已记住”。")
    second_reply = runtime.run("我刚才让你记住的数字是什么？只回复数字。")

    print(f"第一轮回复：{first_reply}")
    print(f"第二轮回复：{second_reply}")
    assert first_reply.strip()
    assert "233" in second_reply
