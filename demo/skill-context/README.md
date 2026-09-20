# 离线 Skill 上下注入示例

这个示例不需要模型 API Key 或外部服务，演示新增的 Skill 子系统如何通过
`ProviderHarness` 以纯数据上下文交给任意 runtime：

- 磁盘 `SKILL.md` 与内联文本分别由 `FilesystemSkillProvider`、`InlineSkillProvider` 加载；
- `SkillRegistry` 汇总来源、检查同名冲突，并在首次 build 时缓存解析结果；
- runtime 未声明 `Capability.SKILLS`，Harness 会注入 L1 技能目录及 L2 定界正文；
- L1 和 L2 都明确标注 `origin` 与 `trust="untrusted"`，防止外部技能覆盖系统指令。

运行：

```bash
python demo/skill-context/demo.py
```

输出中的 `contract.skills.catalog` 是低 token 的常驻目录；
`contract.skills.body.*` 是带 `<skill-content ... trust="untrusted">` 定界的降级正文。

本示例刻意使用不声明 `SKILLS` 的离线 runtime，便于观察降级路径。原生运行时的
`skill__load` 工具注册属于各框架 adapter 的后续增强，不由这个离线示例模拟。