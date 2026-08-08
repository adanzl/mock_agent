# 注意事项

## SSH

主机名：

- 局域网 主机: mini  
- 广域网 主机：vip.sy.frp.one:57904
- 广域网 主机：57c42474b0ea.ofalias.net:58186
用户名 leo
密码 见.env 里的 SSH_PASSWORD
优先级从上到下

## 项目

- 目录：/mnt/data/project/mock_agent
- 服务 mock-agent

## Python 环境

使用 **conda**，env: flask_env，不要再说本地没python了

## 注意

- 重跑任务需要我明确提出了才进行，如果你想重跑需要我二次确认
- 创建的临时文件记得删除
- 重启服务要找我确认
- 变量定义注意拼写，避免cSpell告警
- 新建独立文件要找我二次确认，没必要不要做
- 使用Tailwind
- 给出的回答要有根据，别瞎猜
- 除非特殊说明，日志和数据都去查远程的
- 你要是说服务器就旧代码先去服务器上查git记录再说
- 不要用powershell命令执行远程查询
- 要测试先本地测通过了再推送，除非我要求，不要远程测试
- PowerShell 会拆坏远程 Python，不要直接用
- 接口改了要改文档，文档记得修告警

## 快捷命令

- push 表示执行提交git 并执行push，不用你管pull的事

## 文档

- DeepSeek 接口调用：`docs/deepseek-api.md`（接口变更时同步更新）
- ChatGPT 接口调用：`docs/chatgpt-api.md`（接口变更时同步更新）
