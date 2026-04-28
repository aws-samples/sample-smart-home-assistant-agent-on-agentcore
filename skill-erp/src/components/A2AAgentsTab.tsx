import React, { useState, useEffect, useCallback } from 'react';
import {
  listMyA2aAgents,
  createMyA2aAgent,
  updateMyA2aAgent,
  deleteMyA2aAgent,
  MyA2aRecord,
  A2ACard,
  A2ASkill,
} from '../api/erpApi';
import { useI18n } from '../i18n';

const NAME_RE = /^[a-z][a-z0-9-]{0,62}$/;
const URL_RE = /^https?:\/\/.+/;

const emptySkill = (): A2ASkill => ({ id: '', name: '', description: '', examples: [] });

const emptyCard = (): A2ACard => ({
  name: '',
  description: '',
  endpoint: '',
  version: '1.0.0',
  provider: '',
  capabilities: { streaming: false, pushNotifications: false, stateTransitionHistory: false },
  auth: 'none',
  tags: [],
  skills: [emptySkill()],
});

const A2AAgentsTab: React.FC = () => {
  const { t } = useI18n();
  const [records, setRecords] = useState<MyA2aRecord[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<A2ACard>(emptyCard());
  const [tagsInput, setTagsInput] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<MyA2aRecord | null>(null);

  const clearMsg = () => { setError(''); setSuccess(''); };

  const load = useCallback(async () => {
    setIsLoading(true);
    setError('');
    try {
      setRecords(await listMyA2aAgents());
    } catch (err: any) {
      setError(err.message);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const startCreate = () => {
    clearMsg();
    setForm(emptyCard());
    setTagsInput('');
    setEditingId(null);
    setShowForm(true);
  };

  const startEdit = (r: MyA2aRecord) => {
    clearMsg();
    setForm(r.card);
    setTagsInput((r.card.tags || []).join(', '));
    setEditingId(r.recordId);
    setShowForm(true);
  };

  const cancel = () => { setShowForm(false); setEditingId(null); };

  const validate = (): string | null => {
    if (!NAME_RE.test(form.name)) return t('erp.a2a.validation.name');
    if (!form.description.trim()) return t('erp.a2a.validation.description');
    if (!URL_RE.test(form.endpoint)) return t('erp.a2a.validation.endpoint');
    if (!form.version.trim()) return t('erp.a2a.validation.version');
    if (form.skills.length < 1) return t('erp.a2a.validation.skills');
    for (const s of form.skills) {
      if (!NAME_RE.test(s.id)) return t('erp.a2a.validation.skillId');
      if (!s.name.trim()) return t('erp.a2a.validation.skillName');
    }
    return null;
  };

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    clearMsg();
    const err = validate();
    if (err) { setError(err); return; }
    const payload: A2ACard = {
      ...form,
      tags: tagsInput.split(',').map((x) => x.trim()).filter(Boolean),
    };
    try {
      if (editingId) {
        await updateMyA2aAgent(editingId, payload);
        setSuccess(t('erp.a2a.form.saved'));
      } else {
        await createMyA2aAgent(payload);
        setSuccess(t('erp.a2a.form.savedSubmitted'));
      }
      setShowForm(false);
      setEditingId(null);
      load();
    } catch (ex: any) {
      setError(ex.message);
    }
  };

  const onDelete = async () => {
    if (!deleteTarget) return;
    clearMsg();
    try {
      await deleteMyA2aAgent(deleteTarget.recordId);
      setSuccess(`${t('erp.a2a.form.deleted')}: ${deleteTarget.name}`);
      setDeleteTarget(null);
      load();
    } catch (ex: any) {
      setError(ex.message);
      setDeleteTarget(null);
    }
  };

  const statusLabel = (s: string) => {
    const k = `status.${s}`;
    const v = t(k);
    return v === k ? s : v;
  };

  const updateSkill = (idx: number, patch: Partial<A2ASkill>) => {
    const next = form.skills.map((s, i) => (i === idx ? { ...s, ...patch } : s));
    setForm({ ...form, skills: next });
  };

  return (
    <div className="skill-manager">
      <div className="toolbar">
        <div className="toolbar-left">
          <span className="toolbar-label">{t('erp.a2a.list.title')}</span>
        </div>
        <div className="toolbar-right">
          <button className="btn btn-secondary btn-sm" onClick={load}>
            {t('toolbar.refresh')}
          </button>
          <button className="btn btn-primary" onClick={startCreate}>
            {t('erp.a2a.list.create')}
          </button>
        </div>
      </div>
      <p className="toolbar-hint">{t('erp.a2a.list.hint')}</p>

      {error && <div className="alert alert-error">{error}</div>}
      {success && <div className="alert alert-success">{success}</div>}

      {deleteTarget && (
        <div className="modal-overlay" onClick={() => setDeleteTarget(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3>{t('table.delete')}</h3>
            <p>
              {t('form.deleteConfirm')} <strong>{deleteTarget.name}</strong>?
            </p>
            <div className="modal-actions">
              <button className="btn btn-secondary" onClick={() => setDeleteTarget(null)}>
                {t('form.cancel')}
              </button>
              <button className="btn btn-danger" onClick={onDelete}>
                {t('table.delete')}
              </button>
            </div>
          </div>
        </div>
      )}

      {showForm && (
        <div className="skill-form-container">
          <h3>{editingId ? t('erp.a2a.form.editTitle') : t('erp.a2a.form.createTitle')}</h3>
          <form onSubmit={onSubmit}>
            <div className="form-group">
              <label>{t('erp.a2a.form.name')} <span className="field-required">*</span></label>
              <input
                type="text"
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value.toLowerCase() })}
                disabled={!!editingId}
                placeholder="weather-advisor-agent"
              />
              <div className="field-hint">{t('erp.a2a.form.nameHint')}</div>
            </div>
            <div className="form-group">
              <label>{t('erp.a2a.form.description')} <span className="field-required">*</span></label>
              <input
                type="text"
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                maxLength={500}
              />
            </div>
            <div className="form-row">
              <div className="form-group">
                <label>{t('erp.a2a.form.endpoint')} <span className="field-required">*</span></label>
                <input
                  type="text"
                  value={form.endpoint}
                  onChange={(e) => setForm({ ...form, endpoint: e.target.value })}
                  placeholder="https://example.com/a2a/..."
                />
              </div>
              <div className="form-group">
                <label>{t('erp.a2a.form.version')} <span className="field-required">*</span></label>
                <input
                  type="text"
                  value={form.version}
                  onChange={(e) => setForm({ ...form, version: e.target.value })}
                  placeholder="1.0.0"
                />
              </div>
            </div>
            <div className="form-row">
              <div className="form-group">
                <label>{t('erp.a2a.form.provider')}</label>
                <input
                  type="text"
                  value={form.provider}
                  onChange={(e) => setForm({ ...form, provider: e.target.value })}
                />
              </div>
              <div className="form-group">
                <label>{t('erp.a2a.form.auth')} <span className="field-required">*</span></label>
                <select
                  value={form.auth}
                  onChange={(e) => setForm({ ...form, auth: e.target.value as any })}
                >
                  <option value="none">none</option>
                  <option value="bearer">bearer</option>
                  <option value="apiKey">apiKey</option>
                </select>
              </div>
            </div>
            <div className="form-group">
              <label>{t('erp.a2a.form.capabilities')}</label>
              <div className="capability-row">
                {(['streaming', 'pushNotifications', 'stateTransitionHistory'] as const).map((k) => (
                  <label key={k} className="capability-checkbox">
                    <input
                      type="checkbox"
                      checked={form.capabilities[k]}
                      onChange={(e) => setForm({
                        ...form,
                        capabilities: { ...form.capabilities, [k]: e.target.checked },
                      })}
                    />
                    <span>{k}</span>
                  </label>
                ))}
              </div>
            </div>
            <div className="form-group">
              <label>{t('erp.a2a.form.tags')}</label>
              <input
                type="text"
                value={tagsInput}
                onChange={(e) => setTagsInput(e.target.value)}
                placeholder="energy, demo, smarthome"
              />
              <div className="field-hint">{t('erp.a2a.form.tagsHint')}</div>
            </div>
            <div className="form-section-label">{t('erp.a2a.form.skillsSection')}</div>
            {form.skills.map((s, i) => (
              <div key={i} className="a2a-skill-block">
                <div className="form-row">
                  <div className="form-group">
                    <label>{t('erp.a2a.form.skillId')}</label>
                    <input
                      type="text"
                      value={s.id}
                      onChange={(e) => updateSkill(i, { id: e.target.value.toLowerCase() })}
                    />
                  </div>
                  <div className="form-group">
                    <label>{t('erp.a2a.form.skillName')}</label>
                    <input
                      type="text"
                      value={s.name}
                      onChange={(e) => updateSkill(i, { name: e.target.value })}
                    />
                  </div>
                </div>
                <div className="form-group">
                  <label>{t('erp.a2a.form.skillDescription')}</label>
                  <input
                    type="text"
                    value={s.description}
                    onChange={(e) => updateSkill(i, { description: e.target.value })}
                  />
                </div>
                <div className="form-group">
                  <label>{t('erp.a2a.form.skillExamples')}</label>
                  <input
                    type="text"
                    value={(s.examples || []).join(' | ')}
                    onChange={(e) => updateSkill(i, {
                      examples: e.target.value.split('|').map((x) => x.trim()).filter(Boolean),
                    })}
                    placeholder="first example | second example"
                  />
                  <div className="field-hint">{t('erp.a2a.form.skillExamplesHint')}</div>
                </div>
                {form.skills.length > 1 && (
                  <button
                    type="button"
                    className="btn btn-sm btn-danger"
                    onClick={() => setForm({ ...form, skills: form.skills.filter((_, k) => k !== i) })}
                  >
                    {t('erp.a2a.form.removeSkill')}
                  </button>
                )}
              </div>
            ))}
            <button
              type="button"
              className="btn btn-sm btn-secondary"
              onClick={() => setForm({ ...form, skills: [...form.skills, emptySkill()] })}
            >
              {t('erp.a2a.form.addSkill')}
            </button>
            <div className="form-actions">
              <button type="button" className="btn btn-secondary" onClick={cancel}>
                {t('form.cancel')}
              </button>
              <button type="submit" className="btn btn-primary">
                {editingId ? t('form.save') : t('form.create')}
              </button>
            </div>
          </form>
        </div>
      )}

      {!showForm && (
        <div className="records-table-container">
          {isLoading ? (
            <div className="loading">{t('common.loading')}</div>
          ) : records.length === 0 ? (
            <div className="empty-state">
              <p>{t('erp.a2a.list.empty')}</p>
              <p className="empty-hint">{t('erp.a2a.list.emptyHint')}</p>
            </div>
          ) : (
            <table className="records-table">
              <thead>
                <tr>
                  <th>{t('table.name')}</th>
                  <th>{t('table.description')}</th>
                  <th>{t('erp.a2a.list.colEndpoint')}</th>
                  <th>{t('table.status')}</th>
                  <th>{t('table.updated')}</th>
                  <th>{t('table.actions')}</th>
                </tr>
              </thead>
              <tbody>
                {records.map((r) => (
                  <tr key={r.recordId}>
                    <td className="cell-name">{r.name}</td>
                    <td className="cell-desc">{r.description}</td>
                    <td className="cell-desc">{r.card.endpoint}</td>
                    <td className="cell-status">
                      <span className={`status-badge status-${r.status.toLowerCase()}`}>
                        {statusLabel(r.status)}
                      </span>
                    </td>
                    <td className="cell-date">
                      {r.updatedAt ? new Date(r.updatedAt).toLocaleDateString() : '-'}
                    </td>
                    <td className="cell-actions">
                      <button className="btn btn-sm btn-secondary" onClick={() => startEdit(r)}>
                        {t('table.edit')}
                      </button>
                      <button className="btn btn-sm btn-danger" onClick={() => setDeleteTarget(r)}>
                        {t('table.delete')}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
};

export default A2AAgentsTab;
