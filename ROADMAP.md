# 文件定位

此文件全程由用户维护与更新，不可让ai参与，负责记录现有实现内容与项目规划

## pr审查链路

技术栈：PostgreSQL（适合高并发写入）

review workflow
pr->reviewers->judge->反复循环调整（尚未实现）->report  进入文档库供对话agent查看

review完整接收diff，judge完整接收候选，不做分块处理，过大时reviewer拒绝judge跳过并标error


## 模型参数配置
1. 复用nanobot的agent与subagent逻辑
2. 允许reviewer、judge单独配置参数
3. 控制reviewer、judge最大轮数为30

## multiagent通信

采用黑板模式，由coordinator接收reviewer、judge返回的json结构化数据并写blackboard

## 上下文治理
1. 复用nanobot会话级压缩机制
2. 会话级memory采用增量式更新：将 旧memory+历史记录（全量记录能命中缓存而增量不行） 发forkagent生成长度有限的新meory，作为当前工作状态快照 
注：claude中让模型强迫调用工具编辑内容而非返回文本程序写入，因为调用工具输出更少且内容更加精确 


## 自进化

使用dream逻辑执行跨会话整理，并将内容经人工筛选后注入rag




















