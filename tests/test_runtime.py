"""Runtime 集成测试，验证不使用工具时的真实多轮模型交互。"""

from backend.providers import create_model_client
from backend.runtime import AgentLoop, ContextEngine, Runtime


def test_runtime_multi_turn_chat_without_tools() -> None:
    """执行两轮真实对话，并验证 Runtime 保留了上一轮消息。

    Returns:
        None；模型未返回有效结果或未保留上下文时由断言报告失败。
    """
    client, model = create_model_client()
    runtime = Runtime(
        agent_loop=AgentLoop(client=client, model=model),
        context_engine=ContextEngine(),
    )

    first_reply = runtime.run("请记住数字 233，并只回复“已记住”。")
    second_reply = runtime.run("我刚才让你记住的数字是什么？只回复数字。")

    print(f"第一轮回复：{first_reply}")
    print(f"第二轮回复：{second_reply}")
    assert first_reply.strip()
    assert "233" in second_reply
