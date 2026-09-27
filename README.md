# 博物馆藏品来源与返还审查

标准库实现、SQLite 持久化的独立项目。它管理藏品、历史流转事件、来源引用、证据、权利主张和审查阶段，并提供面向公众、主张人、审查员和工作人员的分层视图。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

访问 <http://127.0.0.1:8103>。数据库默认是 `provenance.db`。测试命令：

```bash
python3 -m unittest -v
```

演示身份通过 `X-User-Id` 传入：`staff`、`reviewer1`、`claimant1`、`public`。

## 主要接口

- `POST /api/objects`、`GET /api/objects`、`GET /api/objects/{id}`：藏品登记与分层查看。
- `POST /api/objects/{id}/update`：更新藏品并创建完整快照。
- `POST /api/sources`、`POST /api/objects/{id}/events`：来源与流转事件。
- `POST /api/objects/{id}/evidence`：上传证据，服务端计算 SHA-256。
- `POST /api/objects/{id}/claims`：提交权利主张。
- `POST /api/claims/{id}/transition`：按 `submitted → under_review → negotiating → resolved_return/rejected` 流转。
- `POST /api/claims/{id}/supplements`：审查员针对主张提出补件要求，登记材料类别与说明。
- `POST /api/supplements/{id}/respond`：主张人上传一份或几份材料回应补件项（Base64，服务端计算 SHA-256）。
- `POST /api/supplements/{id}/review`：审查员查看材料并填写核查意见后关闭补件项。
- `GET /api/supplement-materials/download/{id}`：审查员/工作人员或对应主张人下载补件材料。
- `GET /api/objects/{id}/history` 与 `/history/{version}`：版本历史及历史快照。

公众看不到持有人和内部事件；主张人只能查看自己的主张；阶段不能跳跃或从终态重新打开；每次对象变化都会保存 JSON 快照和审计记录。

## 补件流程规则

- 补件项状态为 `open（待补件）→ submitted（已回应待核查）→ closed（已核查关闭）`，可多次追加材料。
- 主张存在未关闭补件项时，审查员不能把主张推进到 `negotiating` 或 `resolved_return`，接口返回 `409 supplement_pending` 并在 `details.missing_categories` 中列出还缺的材料类别；`rejected`（驳回）不受影响。
- 主张人不能自行关闭补件项：只有审查员在已有材料且填写核查意见（≥5 字）后才能结束该项。
- 主张终态（完成返还/驳回）后不能再提出补件或上传材料。
- 公众只看到主张带有“补件中”标记，看不到材料类别、说明、材料内容和核查意见；主张人只能看到自己主张下的补件项。
- 演示页可切换 `reviewer1` / `claimant1` / `public` 角色，完整走完提出补件、上传回应、核查关闭与流转拦截；每次补件动作都会产生新版本，历史快照保留补件项与材料元数据（不含材料二进制）。
