# 工业专网能力预约与违约归因平台

管理园区网络能力预约、运行观测和服务责任。

## 工程约定

项目采用 Python 包目录组织服务端代码。领域模型、应用服务、持久化适配和接口层应保持边界清晰；时间、标识生成及外部观测均通过可替换端口接入，便于稳定复现业务过程。运行数据不得写入源码目录，临时文件和本地配置由 `.gitignore` 排除。

## 测试

在项目根目录执行：

```bash
python3 -m unittest discover -s tests -v
```

## 编译检查

在项目根目录执行：

```bash
python3 -m compileall -q industrial_capacity tests
```
