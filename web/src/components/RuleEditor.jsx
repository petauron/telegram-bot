import { CheckCircle2, Save } from './Icons'

export function RuleEditor({ form, onChange, onSave, saving, expanded = false }) {
  if (!form) return null
  const update = (key) => (event) => {
    const value = event.target.value
    onChange((current) => ({ ...current, [key]: value }))
  }
  return (
    <form className="form-surface rule-editor" onSubmit={(event) => { event.preventDefault(); onSave() }}>
      <header className="form-surface-header">
        <div><h3>资讯规则</h3><p>规则分仅用于审计；模型启用后，推送判断以 AI 分为准。</p></div>
        <span className="save-state saved"><CheckCircle2 size={15} />保存后立即生效</span>
      </header>
      <section className="form-section rule-fields">
        <label className="field full">
          <span>重要关键词</span>
          <textarea value={form.keywords} onChange={update('keywords')} rows={expanded ? 5 : 3} placeholder="紧急, 故障, 维护" />
          <small>多个关键词使用逗号分隔；只作为已通过资讯分类消息的兴趣线索。</small>
        </label>
        <label className="field">
          <span>可信发送者 ID</span>
          <textarea value={form.trustedIds} onChange={update('trustedIds')} rows={3} placeholder="123456789, 987654321" />
          <small>多个 Telegram 用户 ID 使用逗号分隔。</small>
        </label>
        <div className="field threshold-field" aria-label="实时推送阈值">
          <span>实时推送阈值</span>
          <div className="score-input fixed"><strong>60</strong><b>分</b></div>
          <small>固定为 60 分；分析、去重与通知整理完成后立即投递，不再生成定时摘要。</small>
        </div>
      </section>
      <footer className="form-actions">
        <p>本地规则、回复人数和本地分不会绕过资讯资格门。</p>
        <button className="button primary" type="submit" disabled={saving}><Save size={16} />{saving ? '保存中…' : '保存资讯规则'}</button>
      </footer>
    </form>
  )
}
