from datetime import timedelta

from fastapi import HTTPException

from .db import event
from .domain import EXPERIMENT_DELTA, MINUTES, Profile, RoundState
from .fieldbook import one
from .pairing import closest_pair, membership_vector

ROUND_VIEW = '''
    SELECT r.*,b.code AS block_code,b.name AS block_name,l.profile,l.revision,
           a.profile AS active_profile, a.revision AS active_revision,
           p.profile AS proposed_profile, p.run_id AS pending_run_id,
           e.code AS experiment_code,
           coalesce(p.profile<>l.profile AND r.state='scheduled' AND r.experiment_id IS NULL, false) AS review_hold
    FROM irrigation_round r JOIN vine_block b ON r.block_id=b.id
    JOIN climate_label l ON r.label_id=l.id
    LEFT JOIN climate_label a ON a.block_id=b.id AND a.state='active'
    LEFT JOIN climate_label p ON p.block_id=b.id AND p.state='proposed'
    LEFT JOIN irrigation_experiment e ON e.id=r.experiment_id
'''

EXPERIMENT_VIEW = '''
    SELECT e.id,e.code,e.state,e.starts_at,e.delta_minutes,e.run_id,e.created_at,bc.cultivar,
           c.id AS control_round_id,c.code AS control_code,c.minutes AS control_minutes,c.state AS control_state,
           bc.code AS control_block_code,bc.name AS control_block_name,
           t.id AS treatment_round_id,t.code AS treatment_code,t.minutes AS treatment_minutes,t.state AS treatment_state,
           bt.code AS treatment_block_code,bt.name AS treatment_block_name,
           hc.brix AS control_brix,ht.brix AS treatment_brix
    FROM irrigation_experiment e
    JOIN irrigation_round c ON c.experiment_id=e.id AND c.arm='control'
    JOIN irrigation_round t ON t.experiment_id=e.id AND t.arm='treatment'
    JOIN vine_block bc ON bc.id=c.block_id
    JOIN vine_block bt ON bt.id=t.block_id
    LEFT JOIN harvest_note hc ON hc.round_id=c.id
    LEFT JOIN harvest_note ht ON ht.round_id=t.id
'''

ARM_SUFFIX = {'control': 'C', 'treatment': 'R'}


def rounds(db):
    return db.execute(ROUND_VIEW + ' ORDER BY r.starts_at,r.code').fetchall()


def existing_round(db, round_id):
    return one(db, ROUND_VIEW + ' WHERE r.id=%s', (round_id,), '灌溉轮次')


def experiments(db):
    return db.execute(EXPERIMENT_VIEW + ' ORDER BY e.id DESC').fetchall()


def experiment_detail(db, experiment_id):
    return one(db, EXPERIMENT_VIEW + ' WHERE e.id=%s', (experiment_id,), '减灌试验')


def scheduling_basis(db, block_id):
    block = one(db, '''
        SELECT b.code,l.id AS label_id,l.profile,p.profile AS proposed_profile
        FROM vine_block b LEFT JOIN climate_label l ON b.id=l.block_id AND l.state='active'
        LEFT JOIN climate_label p ON p.block_id=b.id AND p.state='proposed' WHERE b.id=%s
    ''', (block_id,), '地块')
    if block['proposed_profile'] and block['proposed_profile'] != block['profile']:
        raise HTTPException(409, '新标签需要主管确认，暂缓编排该地块')
    if not block['label_id']:
        raise HTTPException(409, '请先为该地块生成生效群落标签')
    return block


def slot_free(db, block_id, begins, minutes, excluding=0):
    ends = begins + timedelta(minutes=minutes)
    return db.execute('''
        SELECT 1 FROM irrigation_round
        WHERE block_id=%s AND id<>%s AND state<>'cancelled'
          AND starts_at < %s AND starts_at + minutes * interval '1 minute' > %s
        LIMIT 1
    ''', (block_id, excluding, ends, begins)).fetchone() is None


def require_free_slot(db, block_id, begins, minutes, excluding=0):
    if not slot_free(db, block_id, begins, minutes, excluding):
        raise HTTPException(409, '同一地块的灌溉时段重叠')


def schedule(db, data, actor):
    basis = scheduling_basis(db, data.block_id)
    minutes = MINUTES[Profile(basis['profile'])]
    require_free_slot(db, data.block_id, data.starts_at, minutes)
    row = db.execute('''
        INSERT INTO irrigation_round(code,block_id,original_label_id,label_id,starts_at,minutes,state)
        VALUES (%s,%s,%s,%s,%s,%s,'scheduled') RETURNING id
    ''', (data.code, data.block_id, basis['label_id'], basis['label_id'], data.starts_at, minutes)).fetchone()
    event(db, actor, 'irrigation_scheduled', data.code, label_id=basis['label_id'], minutes=minutes,
          starts_at=data.starts_at.isoformat())
    return existing_round(db, row['id'])


def start_experiment(db, data, actor):
    # 失败重试按试验编号幂等返回原配组，不重复开单。
    prior = db.execute('SELECT id FROM irrigation_experiment WHERE code=%s', (data.code,)).fetchone()
    if prior:
        return experiment_detail(db, prior['id'])
    run = db.execute('''
        SELECT id,result FROM climate_run WHERE status='effective' ORDER BY id DESC LIMIT 1
    ''').fetchone()
    if run is None:
        raise HTTPException(409, '暂无生效的识别批次，无法按归属概率配组')
    assignments = {item['block_id']: item for item in (run['result'] or {}).get('assignments', [])}
    blocks = db.execute('''
        SELECT b.id,b.code,b.name,b.cultivar,l.id AS label_id,l.profile
        FROM vine_block b JOIN climate_label l ON l.block_id=b.id AND l.state='active'
        WHERE NOT EXISTS (
            SELECT 1 FROM climate_label p
            WHERE p.block_id=b.id AND p.state='proposed' AND p.profile<>l.profile
        ) ORDER BY b.code
    ''').fetchall()
    candidates = []
    for block in blocks:
        assignment = assignments.get(block['id'])
        if assignment is None:
            continue
        minutes = MINUTES[Profile(block['profile'])]
        if not slot_free(db, block['id'], data.starts_at, minutes):
            continue
        candidates.append({**block, 'minutes': minutes, 'vector': membership_vector(assignment)})
    pair = closest_pair(candidates)
    if pair is None:
        raise HTTPException(409, '该时段没有可配对的同品种空闲地块')
    control, treatment, distance = pair
    trial = db.execute('''
        INSERT INTO irrigation_experiment(code,run_id,starts_at,delta_minutes,state,actor)
        VALUES (%s,%s,%s,%s,'active',%s) RETURNING id
    ''', (data.code, run['id'], data.starts_at, EXPERIMENT_DELTA, actor)).fetchone()
    # 两张试验单在同一事务入账，与常规排灌共用时段锁，先入账者占用时段。
    for arm, block, minutes in (('control', control, control['minutes']),
                                ('treatment', treatment, treatment['minutes'] - EXPERIMENT_DELTA)):
        db.execute('''
            INSERT INTO irrigation_round(code,block_id,original_label_id,label_id,starts_at,minutes,state,experiment_id,arm)
            VALUES (%s,%s,%s,%s,%s,%s,'scheduled',%s,%s)
        ''', (f'{data.code}-{ARM_SUFFIX[arm]}', block['id'], block['label_id'], block['label_id'],
              data.starts_at, minutes, trial['id'], arm))
    event(db, actor, 'experiment_scheduled', data.code, run_id=run['id'], cultivar=control['cultivar'],
          control_block=control['code'], treatment_block=treatment['code'],
          control_minutes=control['minutes'], treatment_minutes=treatment['minutes'] - EXPERIMENT_DELTA,
          distance=round(distance, 6), starts_at=data.starts_at.isoformat())
    return experiment_detail(db, trial['id'])


def replan(db, round_id, data, actor):
    row = existing_round(db, round_id)
    if row['state'] != RoundState.NEEDS_REPLAN:
        raise HTTPException(409, '只有确认受影响的轮次可重新排灌')
    basis = scheduling_basis(db, row['block_id'])
    minutes = MINUTES[Profile(basis['profile'])]
    require_free_slot(db, row['block_id'], data.starts_at, minutes, round_id)
    db.execute('''
        UPDATE irrigation_round SET label_id=%s, starts_at=%s,minutes=%s,state='scheduled' WHERE id=%s
    ''', (basis['label_id'], data.starts_at, minutes, round_id))
    event(db, actor, 'irrigation_replanned', row['code'], reason=data.reason,
          previous_label_id=row['label_id'], previous_minutes=row['minutes'], previous_start=row['starts_at'].isoformat(),
          label_id=basis['label_id'], minutes=minutes, starts_at=data.starts_at.isoformat())
    return existing_round(db, round_id)


def finish(db, round_id, completed_at, actor):
    row = existing_round(db, round_id)
    if row['state'] != RoundState.SCHEDULED or row['review_hold']:
        raise HTTPException(409, '轮次须处于可执行的已排状态，待确认或改排轮次不能登记完灌')
    if completed_at < row['starts_at'] + timedelta(minutes=row['minutes']):
        raise HTTPException(409, '完灌时间早于排定结束时间')
    db.execute("UPDATE irrigation_round SET state='completed',completed_at=%s WHERE id=%s",
               (completed_at, round_id))
    event(db, actor, 'irrigation_completed', row['code'], completed_at=completed_at.isoformat())
    return existing_round(db, round_id)


def cancel(db, round_id, reason, actor):
    row = existing_round(db, round_id)
    if row['state'] not in (RoundState.SCHEDULED, RoundState.NEEDS_REPLAN):
        raise HTTPException(409, '该轮次已完灌或已取消')
    db.execute("UPDATE irrigation_round SET state='cancelled' WHERE id=%s", (round_id,))
    event(db, actor, 'irrigation_cancelled', row['code'], reason=reason)
    if row['experiment_id']:
        void_pair(db, row, reason, actor)
    return existing_round(db, round_id)


def void_pair(db, row, reason, actor):
    trial = db.execute('''
        UPDATE irrigation_experiment SET state='voided',void_reason=%s
        WHERE id=%s AND state='active' RETURNING id,code
    ''', (reason, row['experiment_id'])).fetchone()
    sibling = db.execute('''
        SELECT id,code,state FROM irrigation_round WHERE experiment_id=%s AND id<>%s
    ''', (row['experiment_id'], row['id'])).fetchone()
    released = kept = None
    if sibling and sibling['state'] in (RoundState.SCHEDULED, RoundState.NEEDS_REPLAN):
        # 未灌的配对单一并取消，时段随之释放。
        db.execute("UPDATE irrigation_round SET state='cancelled' WHERE id=%s", (sibling['id'],))
        released = sibling['code']
        event(db, actor, 'irrigation_cancelled', sibling['code'], reason='配对试验单取消，整对作废')
    elif sibling and sibling['state'] == RoundState.COMPLETED:
        kept = sibling['code']
    if trial:
        event(db, actor, 'experiment_voided', trial['code'], reason=reason,
              cancelled=row['code'], released=released, kept=kept)


def observe(db, data, actor):
    basis = existing_round(db, data.round_id)
    if basis['state'] != RoundState.COMPLETED or data.observed_at < basis['completed_at']:
        raise HTTPException(409, '采收观察需要关联已完灌轮次，且观察时间不能早于完灌')
    row = db.execute('''
        INSERT INTO harvest_note(round_id,observed_at,brix,berry_count,note,actor)
        VALUES (%s,%s,%s,%s,%s,%s) RETURNING *
    ''', (data.round_id, data.observed_at, data.brix, data.berry_count, data.note, actor)).fetchone()
    event(db, actor, 'harvest_observed', basis['code'], note_id=row['id'], label_id=basis['label_id'])
    return row


def observations(db):
    return db.execute('''
        SELECT h.*,r.code AS round_code,r.completed_at,b.code AS block_code,b.name AS block_name,
               l.profile,l.revision
        FROM harvest_note h JOIN irrigation_round r ON h.round_id=r.id
        JOIN vine_block b ON r.block_id=b.id JOIN climate_label l ON r.label_id=l.id
        ORDER BY h.observed_at DESC,h.id DESC
    ''').fetchall()
