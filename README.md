# 巨灾保险理赔调度系统

标准库实现的巨灾理赔受理、分级、查勘、复核、紧急预付和最终核定服务，数据保存到 SQLite。

## 运行

要求 Python 3.11+（当前 Python 3.9 环境亦可）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址 `http://127.0.0.1:8207`，默认数据库 `catastrophe_claims.db`。可通过 `--db`、`--host`、`--port` 修改。

## 主要接口

请求头 `X-User`、`X-Role` 表示用户与角色。角色有 `intake`、`adjuster`、`surveyor`、`supervisor`、`auditor`。

- `GET /health`、`GET /api/state`、`GET /api/queue`
- `POST /api/claims`：创建报案并识别重复报案
- `POST /api/claims/triage`：计算优先级和欺诈风险
- `POST /api/claims/assign`：分配查勘人员
- `POST /api/evidence`：添加证据并识别跨案件批量复用
- `POST /api/claims/survey`、`POST /api/claims/submit-review`
- `POST /api/claims/emergency-advance`：仅限监督人员、紧急且未超20%的案件
- `POST /api/claims/finalize`：锁定最终核定结果

### 紧急预付共享额度（按灾害事件）

同一灾害事件的紧急预付共享一笔总额度，台账逻辑见 `advance_pool.py`：

- `POST /api/advance-quota`（主管）：按 `event_id` 录入/调整总额度，调整后不得低于当前已占用，支持 `expected_version` 乐观锁。
- `GET /api/advance-quota?event_id=...`：查看该事件总额度、已占用、剩余、待放行清单及每笔缺口；不带参数返回全部事件汇总。
- 批准预付时仍按预估损失 **20%** 校验，随后按申请先后占用事件额度：剩余足够则直接放款（`held`），不足则停在**待放行**（`waiting`），返回本笔缺口 `advance_shortfall`，不产生付款。
- 案件**拒赔、退回**（`POST /api/claims/return`，主管填写理由，案件回到待分配）**或核定完成**后，该案件未用占用归还台账，系统在同一事务内按 FIFO 自动续放待放行申请；排队期间丧失预付资格（如高风险、超20%）的申请自动取消。更晚申请不越过队头。
- 占用、待放行和续放均持久化在 SQLite（`advance_quotas`、`advance_reservations`），服务重开后自动接续；页面按事件展示额度、占用与待放行清单并自动刷新。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整赔付流程、重复报案、乐观锁冲突、批量伪证识别和角色权限。

## 局限

认证依赖请求头；证据仅校验提交的 SHA-256，不实际保存附件；欺诈规则是原型规则而非精算模型；支付记录可审计，但不连接真实银行、保险核心或气象灾害数据源。
