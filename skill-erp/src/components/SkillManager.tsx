import React, { useState, useEffect, useCallback } from 'react';
import {
  listMyRecords,
  createMyRecord,
  updateMyRecord,
  deleteMyRecord,
  MyRecord,
  CreateRecordInput,
} from '../api/erpApi';
import { useI18n } from '../i18n';

const SKILL_NAME_RE = /^(?!-)(?!.*--)(?!.*-$)[a-z0-9-]{1,64}$/;

interface MetadataEntry {
  key: string;
  value: string;
}

interface FormData {
  skillName: string;
  description: string;
  instructions: string;
  allowedTools: string;
  license: string;
  compatibility: string;
  metadata: MetadataEntry[];
}

const emptyForm: FormData = {
  skillName: '',
  description: '',
  instructions: '',
  allowedTools: '',
  license: '',
  compatibility: '',
  metadata: [],
};

const SkillManager: React.FC = () => {
  const { t } = useI18n();
  const [records, setRecords] = useState<MyRecord[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');

  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<FormData>(emptyForm);
  const [deleteTarget, setDeleteTarget] = useState<MyRecord | null>(null);

  const clearMessages = () => {
    setError('');
    setSuccess('');
  };

  const load = useCallback(async () => {
    setIsLoading(true);
    setError('');
    try {
      const items = await listMyRecords();
      setRecords(items);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const handleCreate = () => {
    clearMessages();
    setForm(emptyForm);
    setEditingId(null);
    setShowForm(true);
  };

  const handleEdit = (r: MyRecord) => {
    clearMessages();
    const metadataEntries: MetadataEntry[] = r.metadata
      ? Object.entries(r.metadata).map(([key, value]) => ({ key, value }))
      : [];
    setForm({
      skillName: r.name,
      description: r.description,
      instructions: r.instructions || '',
      allowedTools: (r.allowedTools || []).join(', '),
      license: r.license || '',
      compatibility: r.compatibility || '',
      metadata: metadataEntries,
    });
    setEditingId(r.recordId);
    setShowForm(true);
  };

  const handleCancel = () => {
    setShowForm(false);
    setEditingId(null);
    setForm(emptyForm);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    clearMessages();

    if (!editingId && !SKILL_NAME_RE.test(form.skillName)) {
      setError(t('form.invalidName'));
      return;
    }
    if (!form.description.trim()) {
      setError(t('form.descriptionRequired'));
      return;
    }

    const allowedTools = form.allowedTools
      .split(',')
      .map((tt) => tt.trim())
      .filter(Boolean);

    const metadata: Record<string, string> = {};
    for (const entry of form.metadata) {
      if (entry.key.trim()) {
        metadata[entry.key.trim()] = entry.value;
      }
    }

    try {
      if (editingId) {
        await updateMyRecord(editingId, {
          description: form.description,
          instructions: form.instructions,
          allowedTools,
          license: form.license,
          compatibility: form.compatibility,
          metadata,
        });
        setSuccess(t('form.saved'));
      } else {
        const input: CreateRecordInput = {
          skillName: form.skillName,
          description: form.description,
          instructions: form.instructions,
          allowedTools,
          license: form.license || undefined,
          compatibility: form.compatibility || undefined,
          metadata: Object.keys(metadata).length > 0 ? metadata : undefined,
        };
        await createMyRecord(input);
        setSuccess(t('form.savedSubmitted'));
      }
      setShowForm(false);
      setEditingId(null);
      setForm(emptyForm);
      load();
    } catch (err: any) {
      setError(err.message);
    }
  };

  const handleDelete = async () => {
    if (!deleteTarget) return;
    clearMessages();
    try {
      await deleteMyRecord(deleteTarget.recordId);
      setSuccess(`${t('form.deleted')}: ${deleteTarget.name}`);
      setDeleteTarget(null);
      load();
    } catch (err: any) {
      setError(err.message);
      setDeleteTarget(null);
    }
  };

  const statusLabel = (status: string) => {
    const key = `status.${status}`;
    const translated = t(key);
    return translated === key ? status : translated;
  };

  return (
    <div className="skill-manager">
      <div className="toolbar">
        <div className="toolbar-left">
          <span className="toolbar-label">{t('toolbar.title')}</span>
        </div>
        <div className="toolbar-right">
          <button className="btn btn-secondary btn-sm" onClick={load}>
            {t('toolbar.refresh')}
          </button>
          <button className="btn btn-primary" onClick={handleCreate}>
            {t('toolbar.newSkill')}
          </button>
        </div>
      </div>

      <p className="toolbar-hint">{t('toolbar.hint')}</p>

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
              <button className="btn btn-danger" onClick={handleDelete}>
                {t('table.delete')}
              </button>
            </div>
          </div>
        </div>
      )}

      {showForm && (
        <div className="skill-form-container">
          <h3>{editingId ? t('form.editTitle') : t('form.createTitle')}</h3>
          <form onSubmit={handleSubmit}>
            <div className="form-group">
              <label>
                {t('form.skillName')} <span className="field-required">*</span>
              </label>
              <input
                type="text"
                value={form.skillName}
                onChange={(e) =>
                  setForm({ ...form, skillName: e.target.value.toLowerCase() })
                }
                disabled={!!editingId}
                placeholder={t('form.skillNamePlaceholder')}
              />
              <div className="field-hint">{t('form.skillNameHint')}</div>
            </div>

            <div className="form-group">
              <label>
                {t('form.description')} <span className="field-required">*</span>
              </label>
              <input
                type="text"
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                placeholder={t('form.descriptionPlaceholder')}
                maxLength={1024}
              />
            </div>

            <div className="form-group">
              <label>{t('form.allowedTools')}</label>
              <input
                type="text"
                value={form.allowedTools}
                onChange={(e) => setForm({ ...form, allowedTools: e.target.value })}
                placeholder={t('form.allowedToolsPlaceholder')}
              />
            </div>

            <div className="form-section-label">{t('form.optional')}</div>

            <div className="form-row">
              <div className="form-group">
                <label>{t('form.license')}</label>
                <input
                  type="text"
                  value={form.license}
                  onChange={(e) => setForm({ ...form, license: e.target.value })}
                  placeholder={t('form.licensePlaceholder')}
                />
              </div>
              <div className="form-group">
                <label>{t('form.compatibility')}</label>
                <input
                  type="text"
                  value={form.compatibility}
                  onChange={(e) =>
                    setForm({ ...form, compatibility: e.target.value })
                  }
                  placeholder={t('form.compatibilityPlaceholder')}
                  maxLength={500}
                />
              </div>
            </div>

            <div className="form-group">
              <label>{t('form.metadata')}</label>
              <div className="metadata-editor">
                {form.metadata.map((entry, i) => (
                  <div key={i} className="metadata-row">
                    <input
                      type="text"
                      className="metadata-key"
                      value={entry.key}
                      onChange={(e) => {
                        const updated = [...form.metadata];
                        updated[i] = { ...updated[i], key: e.target.value };
                        setForm({ ...form, metadata: updated });
                      }}
                      placeholder={t('form.metadataKey')}
                    />
                    <input
                      type="text"
                      className="metadata-value"
                      value={entry.value}
                      onChange={(e) => {
                        const updated = [...form.metadata];
                        updated[i] = { ...updated[i], value: e.target.value };
                        setForm({ ...form, metadata: updated });
                      }}
                      placeholder={t('form.metadataValue')}
                    />
                    <button
                      type="button"
                      className="btn btn-sm btn-danger"
                      onClick={() => {
                        const updated = form.metadata.filter((_, idx) => idx !== i);
                        setForm({ ...form, metadata: updated });
                      }}
                    >
                      {t('form.remove')}
                    </button>
                  </div>
                ))}
                <button
                  type="button"
                  className="btn btn-sm btn-secondary"
                  onClick={() =>
                    setForm({
                      ...form,
                      metadata: [...form.metadata, { key: '', value: '' }],
                    })
                  }
                >
                  {t('form.addEntry')}
                </button>
              </div>
            </div>

            <div className="form-group">
              <label>{t('form.instructions')}</label>
              <textarea
                className="instructions-textarea"
                value={form.instructions}
                onChange={(e) => setForm({ ...form, instructions: e.target.value })}
                placeholder={t('form.instructionsPlaceholder')}
                rows={10}
              />
            </div>

            <div className="form-actions">
              <button type="button" className="btn btn-secondary" onClick={handleCancel}>
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
              <p>{t('table.empty')}</p>
              <p className="empty-hint">{t('table.emptyHint')}</p>
            </div>
          ) : (
            <table className="records-table">
              <thead>
                <tr>
                  <th>{t('table.name')}</th>
                  <th>{t('table.description')}</th>
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
                    <td className="cell-status">
                      <span className={`status-badge status-${r.status.toLowerCase()}`}>
                        {statusLabel(r.status)}
                      </span>
                    </td>
                    <td className="cell-date">
                      {r.updatedAt ? new Date(r.updatedAt).toLocaleDateString() : '-'}
                    </td>
                    <td className="cell-actions">
                      <button className="btn btn-sm btn-secondary" onClick={() => handleEdit(r)}>
                        {t('table.edit')}
                      </button>
                      <button
                        className="btn btn-sm btn-danger"
                        onClick={() => setDeleteTarget(r)}
                      >
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

export default SkillManager;
