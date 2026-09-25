# 巨灾保险理赔调度系统

标准库实现的巨灾理赔受理、分级、查勘、复核、紧急预付和最终核定服务，数据保存到 SQLite。

## 运行

要求 Python 3.11+（当前 Python 3.9 环境亦可）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址 `http://127.0.0.1:8207`，默认数据库 `catastrophe_claims.db`。可通过 `--db`、`--host`、`--port` 修改。

## 事件预付额度池

台风理赔高峰时，同一灾害事件的紧急预付共享一笔总额度，分三层落地：

- `quota_store.py`（额度占用）：事件额度池与预付申请队列的登记、占用、释放和按申请先后放行；写操作与案件更新同事务，重启后状态可接上。
- `app.py`（业务判断）：角色权限、紧急/欺诈校验、每案预估损失 20% 上限，以及拒赔、退回、核定完成时的占用释放。
- `static/index.html`（页面）：按事件查看额度、占用、待放行清单（含缺口），重开页面自动恢复上次查看的事件。

规则：

1. 主管先 `POST /api/events/pool` 录入事件总额度（调低不能低于已占用）。
2. `POST /api/claims/emergency-advance` 批准预付时按申请先后占用额度；剩余不足时申请停在待放行，响应和页面都显示缺口；严格按先后排队，后来的小额申请不插队。
3. 案件拒赔、退回（`POST /api/claims/return`）或核定完成后，未用占用自动释放，等待队列按顺序放行并补记付款；案件结束时其待放行申请一并取消。
4. 调高额度会立即重排等待队列。

## 主要接口

请求头 `X-User`、`X-Role` 表示用户与角色。角色有 `intake`、`adjuster`、`surveyor`、`supervisor`、`auditor`。

- `GET /health`、`GET /api/state`、`GET /api/queue`
- `POST /api/claims`：创建报案并识别重复报案
- `POST /api/claims/triage`：计算优先级和欺诈风险
- `POST /api/claims/assign`：分配查勘人员
- `POST /api/evidence`：添加证据并识别跨案件批量复用
- `POST /api/claims/survey`、`POST /api/claims/submit-review`
- `POST /api/events/pool`：主管录入/调整事件预付总额度
- `GET /api/advance-pools`、`GET /api/advance-pool?event_id=`：按事件查看额度、占用与待放行清单
- `POST /api/claims/emergency-advance`：仅限监督人员、紧急且未超20%的案件；额度不足时进入待放行并返回缺口
- `POST /api/claims/return`：主管退回案件（查勘/复核/升级状态 → 重新查勘），释放其预付占用
- `POST /api/claims/finalize`：锁定最终核定结果，并释放该案件未用的预付占用

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整赔付流程、重复报案、乐观锁冲突、批量伪证识别、角色权限，以及额度池的先后占用、待放行缺口、拒赔/退回/核定释放、队首阻塞和重开恢复。

## 局限

认证依赖请求头；证据仅校验提交的 SHA-256，不实际保存附件；欺诈规则是原型规则而非精算模型；额度池只是记账占用，支付记录可审计，但不连接真实银行、保险核心或气象灾害数据源。
