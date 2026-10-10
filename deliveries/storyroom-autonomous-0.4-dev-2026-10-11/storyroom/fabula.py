"""Explicitly limited Fabula Ultima attribute-check support, not a full ruleset."""
import json
import secrets
from .store_types import fail

ATTRIBUTES = {'DEX', 'INS', 'MIG', 'WLP'}


def validate_card(card):
    attrs = card.get('attributes', {})
    if set(attrs) != ATTRIBUTES or any(type(v) is not int or v not in (6,8,10,12) for v in attrs.values()):
        fail('四项属性 DEX／INS／MIG／WLP 必须为 d6、d8、d10 或 d12')
    resources = card.get('resources', {})
    if set(resources) != {'HP','MP','IP','FP'}:
        fail('角色卡必须包含 HP、MP、IP、FP')
    for key, value in resources.items():
        if not isinstance(value, dict) or set(value) != {'current','max'} or any(type(v) is not int or not 0 <= v <= 999 for v in value.values()) or value['current'] > value['max']:
            fail('资源须包含合法 current／max，当前值不能超过上限')
    traits = card.get('traits', [])
    if not isinstance(traits,list) or len(traits)>3 or any(not isinstance(t,str) or not 1<=len(t)<=200 for t in traits):
        fail('最多三个可调用特质，分别来自身份、主题和出身')
    if set(card) - {'attributes','resources','traits','notes'}:
        fail('此版本只支持属性、资源、特质和备注；不支持自动职业与战斗结算')
    if not isinstance(card.get('notes',''),str) or len(card.get('notes',''))>4000:
        fail('备注过长')
    return card


def evaluate(values, modifier, difficulty):
    fumble = values == [1,1]
    critical = values[0] == values[1] and values[0] >= 6
    total = sum(values)+modifier
    return {'values':values,'modifier':modifier,'total':total,'difficulty':difficulty,
            'fumble':fumble,'critical':critical,'success':not fumble and (critical or total>=difficulty)}


class FabulaFeatures:
    def fu_view(self, db, room):
        return {'ruleset':self.ensure_runtime(db,room)['ruleset'], 'coverage':'属性检定、特质重投、资源台账；未实现职业、战斗、升级与羁绊加值',
                'cards':[{'role':r['role'],'card':json.loads(r['data'])} for r in db.execute('SELECT * FROM fu_cards WHERE room=?',(room,))],
                'checks':[{'task':r['task'],**json.loads(r['data'])} for r in db.execute('SELECT * FROM fu_checks WHERE room=?',(room,))]}

    def set_fu_card(self, room, role, card):
        card=validate_card(card)
        with self.db() as db:
            self.room(db,room)
            runtime=self.ensure_runtime(db,room)
            if runtime['enabled'] and runtime['state']=='RUNNING':
                fail('请先暂停整场游戏再编辑属性卡',409)
            if not db.execute('SELECT 1 FROM roles WHERE room=? AND id=?',(room,role)).fetchone():
                fail('角色不属于此房间',404)
            if db.execute("SELECT 1 FROM tasks WHERE room=? AND status IN ('queued','running','proposed')",(room,)).fetchone():
                fail('请先完成或取消当前任务',409)
            db.execute('INSERT OR REPLACE INTO fu_cards VALUES(?,?,?)',(room,role,json.dumps(card,ensure_ascii=False)))
            db.execute("UPDATE runtime SET ruleset='fabula-attribute-v1' WHERE room=?",(room,))
            db.execute('UPDATE rooms SET version=version+1 WHERE id=?',(room,))
            self.event(db,room,'角色卡更新','主持人',{'角色':role,'最终物语属性卡':card,'支持范围':'属性检定子集，不是完整规则'})
        return {'ok':True}

    def fu_check(self, room, task, attributes, difficulty):
        if len(attributes)!=2 or any(a not in ATTRIBUTES for a in attributes) or type(difficulty) is not int or not 1<=difficulty<=30:
            fail('必须选择两项属性和 1～30 的难度')
        with self.db() as db:
            self.progress_guard(db,room,manual=True)
            state=self.room(db,room)
            row=db.execute("SELECT * FROM tasks WHERE room=? AND id=? AND status='proposed'",(room,task)).fetchone()
            if not row or row['version']!=state['version'] or json.loads(row['result']).get('intent','action')!='action':
                fail('行动不存在或已过时',409)
            if db.execute('SELECT 1 FROM checks WHERE task=?',(task,)).fetchone():
                fail('此行动已使用简化规则检定，不能同时使用两套规则',409)
            existing=db.execute('SELECT data FROM fu_checks WHERE task=?',(task,)).fetchone()
            if existing:
                data=json.loads(existing['data'])
                if data['attributes']!=attributes or data['difficulty']!=difficulty:
                    fail('检定已锁定，不能更改属性或难度',409)
                return {**data,'duplicate':True}
            person=db.execute('SELECT data FROM fu_cards WHERE room=? AND role=?',(room,row['role'])).fetchone()
            if not person:
                fail('请先配置此角色的最终物语属性卡',409)
            card=json.loads(person['data']);sides=[card['attributes'][a] for a in attributes]
            data={**evaluate([self.roll(n)+1 for n in sides],0,difficulty),'attributes':attributes,'sides':sides,'role':row['role'],'rerolls':[],'finalized':False,'choice_lease':'','choice_until':0}
            if data['fumble']:
                fp=card['resources']['FP'];fp['current']=min(fp['max'],fp['current']+1)
                db.execute('UPDATE fu_cards SET data=? WHERE role=?',(json.dumps(card,ensure_ascii=False),row['role']))
            db.execute('INSERT INTO fu_checks VALUES(?,?,?)',(room,task,json.dumps(data,ensure_ascii=False)))
            self.event(db,room,'最终物语检定','平台',{'任务':task,'检定':data,'说明':'实际投骰；玩家可调用特质重投，主持人不能代选'})
        return data

    def claim_fu_choice(self, owner):
        from .store import digest
        with self.db() as db:
            room=owner['room'];runtime=self.ensure_runtime(db,room)
            if runtime['enabled'] and runtime['state']!='RUNNING':
                return None
            for row in db.execute("SELECT c.* FROM fu_checks c JOIN tasks t ON t.id=c.task WHERE c.room=? AND t.role=? AND t.status='proposed' AND t.version=?",(room,owner['id'],self.room(db,room)['version'])).fetchall():
                data=json.loads(row['data'])
                if data['finalized'] or data.get('choice_until',0)>self.clock():
                    continue
                if data.get('choice_lease') and data.get('choice_epoch')==runtime['epoch']:
                    self.gm_fault(db,room,'PC检定选择超时，请检查后恢复')
                    return None
                card=json.loads(db.execute('SELECT data FROM fu_cards WHERE role=?',(owner['id'],)).fetchone()[0])
                if data['fumble'] or not card['traits'] or card['resources']['FP']['current']<1:
                    data['finalized']=True
                    db.execute('UPDATE fu_checks SET data=? WHERE task=?',(json.dumps(data,ensure_ascii=False),row['task']))
                    self.event(db,room,'最终物语检定',owner['name'],{'任务':row['task'],'说明':'无可用特质重投，确认原检定'})
                    return None
                if runtime['enabled']:
                    if runtime['pc_calls']>=runtime['pc_budget']:
                        self.gm_fault(db,room,'PC调用预算已耗尽')
                        return None
                    db.execute('UPDATE runtime SET pc_calls=pc_calls+1 WHERE room=?',(room,))
                lease=secrets.token_urlsafe(24)
                data['choice_lease']=digest(lease);data['choice_until']=self.clock()+120;data['choice_epoch']=runtime['epoch']
                db.execute('UPDATE fu_checks SET data=? WHERE task=?',(json.dumps(data,ensure_ascii=False),row['task']))
                public={k:v for k,v in data.items() if not k.startswith('choice_')}
                return {'task':row['task'],'lease':lease,'check':public,'card':card}
        return None

    def complete_fu_choice(self, owner, task, lease, indices, trait):
        from .store import digest
        with self.db() as db:
            self.progress_guard(db,owner['room'])
            row=db.execute('SELECT c.data,t.role,t.version,t.status FROM fu_checks c JOIN tasks t ON t.id=c.task WHERE c.task=? AND c.room=?',(task,owner['room'])).fetchone()
            if not row or row['role']!=owner['id']:
                fail('检定不属于此玩家',404)
            data=json.loads(row['data']);runtime=self.ensure_runtime(db,owner['room'])
            if row['status']!='proposed' or row['version']!=self.room(db,owner['room'])['version'] or data['choice_epoch']!=runtime['epoch'] or data['choice_until']<=self.clock() or not secrets.compare_digest(data['choice_lease'],digest(lease)):
                fail('选择任务已过时',409)
            if data['finalized']:
                return {'ok':True,'duplicate':True}
            if indices:
                data=self.fu_reroll(owner,task,indices,trait)
            elif trait:
                fail('不重投时特质应留空')
            data['finalized']=True
            db.execute('UPDATE fu_checks SET data=? WHERE task=?',(json.dumps(data,ensure_ascii=False),task))
            self.event(db,owner['room'],'最终物语检定',owner['name'],{'任务':task,'说明':'玩家已确认检定，允许主持人裁决'})
        return {'ok':True}

    def fu_reroll(self, owner, task, indices, trait):
        with self.db() as db:
            room=owner['room'];self.progress_guard(db,room)
            row=db.execute("SELECT t.*,c.data FROM tasks t JOIN fu_checks c ON c.task=t.id WHERE t.room=? AND t.id=? AND t.role=? AND t.status='proposed'",(room,task,owner['id'])).fetchone()
            if not row or row['version']!=self.room(db,room)['version']:
                fail('检定不属于当前玩家或已过时',409)
            data=json.loads(row['data'])
            if data['finalized'] or data['fumble'] or data['rerolls']:
                fail('不能在大失败、结算后或已经重投后再重投',409)
            if not indices or len(set(indices))!=len(indices) or any(type(i) is not int or i not in (0,1) for i in indices):
                fail('选择第一颗、第二颗或两颗骰子')
            card=json.loads(db.execute('SELECT data FROM fu_cards WHERE role=?',(owner['id'],)).fetchone()[0])
            if trait not in card['traits'] or card['resources']['FP']['current']<1:
                fail('特质不存在或物语点不足',409)
            old=list(data['values']);values=list(old)
            for i in indices: values[i]=self.roll(data['sides'][i])+1
            data.update(evaluate(values,data['modifier'],data['difficulty']))
            data['rerolls'].append({'before':old,'trait':trait,'indices':indices})
            card['resources']['FP']['current']-=1
            if data['fumble']:
                card['resources']['FP']['current']=min(card['resources']['FP']['max'],card['resources']['FP']['current']+1)
            db.execute('UPDATE fu_cards SET data=? WHERE role=?',(json.dumps(card,ensure_ascii=False),owner['id']))
            db.execute('UPDATE fu_checks SET data=? WHERE task=?',(json.dumps(data,ensure_ascii=False),task))
            self.event(db,room,'最终物语检定',owner['name'],{'任务':task,'检定':data,'说明':'由玩家选择特质，消费1物语点重投'})
        return data
