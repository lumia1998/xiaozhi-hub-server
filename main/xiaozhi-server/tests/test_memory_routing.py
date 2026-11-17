"""Pure routing boundaries: no network, model calls, or persistent user data."""
from pathlib import Path

import pytest
import yaml

from core.agent.memory_routing import recall_decision, shared_tools_for_turn


READ = "recall_shared_memory"
WRITE = "remember_shared_memory"
STATUS = "shared_memory_status"
DELETE = "forget_shared_memory"
CFG = {"hindsight": {"enabled": True, "asset_aliases": ["PVE", "天枢"]}}
DEPLOYED_CFG = {"hindsight": yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text(encoding="utf-8"))["hindsight"]}


@pytest.fixture(params=[CFG, DEPLOYED_CFG], ids=["module-default-index", "repository-default-config"])
def routing_config(request):
    return request.param


@pytest.mark.parametrize("text", [
    "我的服务器都部署了什么？",
    "我的服务器目前是什么配置？",
    "我家的内网是什么网段？",
    "30机器上的模型网关在哪个端口？",
    "192.0.2.60上的Hindsight是什么配置？",
    "PVE之前跑着哪些虚拟机？",
    "我们之前给Docker配置了什么网络？",
    "我的天气服务部署在哪台服务器？",
    "先关灯，再查我的服务器部署了什么",
])
def test_personal_environment_facts_allow_only_recall(text, routing_config):
    decision = recall_decision(text, routing_config)
    assert decision["allowed"], decision
    assert decision["topic"]
    assert shared_tools_for_turn(text, routing_config) == {READ}


@pytest.mark.parametrize("text", [
    "什么是服务器？",
    "怎么配置路由器？",
    "解释Docker的原理",
    "什么是共享记忆？",
    "什么是Hindsight？",
    "Hindsight怎么保存记忆",
    "你觉得Hindsight有用吗",
    "推荐一台服务器",
    "今天天气怎么样？",
    "我家今天天气怎么样？",
    "帮我看看明天要不要带伞",
    "关闭客厅主灯",
    "把服务器旁边的台灯关掉",
    "我的服务器旁边的灯现在亮着吗？",
    "今天的天气和昨天比如何？",
    "你好",
    "讲一个笑话",
    "我今天心情不错",
    "ship有哪些功能？",
])
def test_daily_chat_and_general_knowledge_do_not_offer_shared_memory(text, routing_config):
    assert not recall_decision(text, routing_config)["allowed"]
    assert shared_tools_for_turn(text, routing_config) == set()


@pytest.mark.parametrize("text", [
    "不要查记忆，我的服务器是什么配置？",
    "我的服务器部署在哪，不用查共享记忆",
    "别访问Hindsight，解释一下服务器",
    "不用记忆，30机器是什么配置",
])
def test_current_explicit_opt_out_overrides_topic_and_prior(text, routing_config):
    previous = "我的服务器都部署了什么？"
    assert not recall_decision(text, routing_config, previous)["allowed"]
    assert shared_tools_for_turn(text, routing_config, previous) == set()


@pytest.mark.parametrize("text,expected", [
    ("查询共享记忆里的回答风格偏好", {READ}),
    ("你还记得我之前保存的偏好吗？", {READ}),
    ("你还记得之前保存的共享偏好吗", {READ}),
    ("查之前保存的共享记忆", {READ}),
    ("请记住共享偏好：回答先说结论", {WRITE, STATUS}),
    ("把回答先说结论保存到共享记忆", {WRITE, STATUS}),
    ("请把这条偏好保存到共享记忆：先给结论", {WRITE, STATUS}),
    ("不要保存共享记忆", set()),
    ("刚才的共享记忆保存完成了吗？", {STATUS}),
    ("删除刚才保存的共享记忆", {READ, DELETE, STATUS}),
    ("不要删除这条共享记忆", set()),
])
def test_explicit_shared_operations_remain_separate_from_topic_index(text, expected, routing_config):
    assert shared_tools_for_turn(text, routing_config) == expected


@pytest.mark.parametrize("text", ["那台呢？", "它的IP呢？", "它的配置是什么？"])
def test_immediate_short_reference_can_follow_personal_environment(text, routing_config):
    result = recall_decision(text, routing_config, "我的服务器都部署了什么？")
    assert result["allowed"]
    assert shared_tools_for_turn(text, routing_config, "我的服务器都部署了什么？") == {READ}


@pytest.mark.parametrize("text,previous", [
    ("今天天气怎么样？", "我的服务器都部署了什么？"),
    ("关闭客厅灯", "我的服务器都部署了什么？"),
    ("你好", "我的服务器都部署了什么？"),
    ("它的IP呢？", "今天天气怎么样？"),
    ("那台呢？", "它的IP呢？"),
    ("它呢？", "什么是服务器？"),
    ("它的配置呢？", "不要查记忆，我的服务器是什么配置？"),
])
def test_context_does_not_leak_to_daily_requests_or_chain_indefinitely(text, previous, routing_config):
    assert not recall_decision(text, routing_config, previous)["allowed"]
    assert shared_tools_for_turn(text, routing_config, previous) == set()


def test_short_shared_status_and_delete_only_follow_explicit_shared_turn(routing_config):
    previous = "请把回答先说结论保存到共享记忆"
    assert shared_tools_for_turn("完成了吗？", routing_config, previous) == {STATUS}
    assert shared_tools_for_turn("删除那条", routing_config, previous) == {READ, DELETE, STATUS}
    assert shared_tools_for_turn("完成了吗？", routing_config, "我的服务器部署了什么？") == set()
    assert shared_tools_for_turn("删除那条", routing_config, "今天天气怎么样？") == set()


def test_topic_index_override_and_asset_aliases_are_local_configuration():
    config = {"hindsight": {"recall_topics": {"database": ["数据库"]}, "asset_aliases": ["天枢"]}}
    answer = recall_decision("天枢数据库是什么版本？", config)
    assert answer["allowed"] and answer["topic"] == "database"
    assert not recall_decision("我的服务器什么配置？", config)["allowed"]


@pytest.mark.parametrize("text", [None, "", "   ", 17, {}, []])
def test_missing_or_invalid_text_is_inert(text, routing_config):
    assert not recall_decision(text, routing_config)["allowed"]
    assert shared_tools_for_turn(text, routing_config) == set()
