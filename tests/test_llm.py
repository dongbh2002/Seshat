"""模型客户端集成测试，用于验证真实 LLM 对话是否可用。"""

from backend.config import config
from backend.providers import create_model_client


def test_llm_chat_with_hello() -> None:
    """向当前配置的模型发送“你好”，并验证返回内容非空。

    Returns:
        None；模型无有效回复时由断言报告失败。
    """
    client, model = create_model_client()
    model_config = config["models"][config["current_model"]]

    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "你好"}],
        **model_config.get("parameters", {}),
    )

    content = response.choices[0].message.content
    print(f"LLM 回复：{content}")
    assert content and content.strip()
