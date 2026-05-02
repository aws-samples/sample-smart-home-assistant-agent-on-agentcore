import React, { useState, useEffect, useCallback } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Badge from '@cloudscape-design/components/badge';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Container from '@cloudscape-design/components/container';
import Form from '@cloudscape-design/components/form';
import FormField from '@cloudscape-design/components/form-field';
import Header from '@cloudscape-design/components/header';
import Input from '@cloudscape-design/components/input';
import Modal from '@cloudscape-design/components/modal';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Table from '@cloudscape-design/components/table';
import Textarea from '@cloudscape-design/components/textarea';
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

  useEffect(() => { load(); }, [load]);

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

  const handleSubmit = async () => {
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

  const renderStatus = (status: string) => {
    const label = statusLabel(status);
    const s = status.toUpperCase();
    if (s === 'APPROVED' || s === 'PUBLISHED') return <StatusIndicator type="success">{label}</StatusIndicator>;
    if (s === 'PENDING' || s === 'REVIEW') return <StatusIndicator type="pending">{label}</StatusIndicator>;
    if (s === 'REJECTED') return <StatusIndicator type="error">{label}</StatusIndicator>;
    return <Badge>{label}</Badge>;
  };

  return (
    <SpaceBetween size="l">
      {error && <Alert type="error" dismissible onDismiss={() => setError('')}>{error}</Alert>}
      {success && <Alert type="success">{success}</Alert>}

      {deleteTarget && (
        <Modal
          visible
          onDismiss={() => setDeleteTarget(null)}
          header={t('table.delete')}
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button onClick={() => setDeleteTarget(null)}>{t('form.cancel')}</Button>
                <Button variant="primary" onClick={handleDelete}>{t('table.delete')}</Button>
              </SpaceBetween>
            </Box>
          }
        >
          <p>{t('form.deleteConfirm')} <strong>{deleteTarget.name}</strong>?</p>
        </Modal>
      )}

      {showForm && (
        <Container
          header={
            <Header variant="h2">
              {editingId ? t('form.editTitle') : t('form.createTitle')}
            </Header>
          }
        >
          <form onSubmit={(e) => { e.preventDefault(); void handleSubmit(); }}>
            <Form
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button onClick={handleCancel}>{t('form.cancel')}</Button>
                  <Button variant="primary" formAction="submit">
                    {editingId ? t('form.save') : t('form.create')}
                  </Button>
                </SpaceBetween>
              }
            >
              <SpaceBetween size="m">
                <FormField
                  label={<>{t('form.skillName')} <span style={{ color: '#d91515' }}>*</span></>}
                  description={t('form.skillNameHint')}
                >
                  <Input
                    value={form.skillName}
                    onChange={({ detail }) => setForm({ ...form, skillName: detail.value.toLowerCase() })}
                    disabled={!!editingId}
                    placeholder={t('form.skillNamePlaceholder')}
                  />
                </FormField>
                <FormField label={<>{t('form.description')} <span style={{ color: '#d91515' }}>*</span></>}>
                  <Input
                    value={form.description}
                    onChange={({ detail }) => setForm({ ...form, description: detail.value })}
                    placeholder={t('form.descriptionPlaceholder')}
                  />
                </FormField>
                <FormField label={t('form.allowedTools')}>
                  <Input
                    value={form.allowedTools}
                    onChange={({ detail }) => setForm({ ...form, allowedTools: detail.value })}
                    placeholder={t('form.allowedToolsPlaceholder')}
                  />
                </FormField>
                <Box color="text-body-secondary" fontSize="body-s">{t('form.optional')}</Box>
                <FormField label={t('form.license')}>
                  <Input
                    value={form.license}
                    onChange={({ detail }) => setForm({ ...form, license: detail.value })}
                    placeholder={t('form.licensePlaceholder')}
                  />
                </FormField>
                <FormField label={t('form.compatibility')}>
                  <Input
                    value={form.compatibility}
                    onChange={({ detail }) => setForm({ ...form, compatibility: detail.value })}
                    placeholder={t('form.compatibilityPlaceholder')}
                  />
                </FormField>
                <FormField label={t('form.metadata')}>
                  <SpaceBetween size="xs">
                    {form.metadata.map((entry, i) => (
                      <SpaceBetween key={i} direction="horizontal" size="xs">
                        <Input
                          value={entry.key}
                          onChange={({ detail }) => {
                            const updated = [...form.metadata];
                            updated[i] = { ...updated[i], key: detail.value };
                            setForm({ ...form, metadata: updated });
                          }}
                          placeholder={t('form.metadataKey')}
                        />
                        <Input
                          value={entry.value}
                          onChange={({ detail }) => {
                            const updated = [...form.metadata];
                            updated[i] = { ...updated[i], value: detail.value };
                            setForm({ ...form, metadata: updated });
                          }}
                          placeholder={t('form.metadataValue')}
                        />
                        <Button
                          onClick={() => {
                            const updated = form.metadata.filter((_, idx) => idx !== i);
                            setForm({ ...form, metadata: updated });
                          }}
                        >
                          {t('form.remove')}
                        </Button>
                      </SpaceBetween>
                    ))}
                    <Button
                      onClick={() => setForm({ ...form, metadata: [...form.metadata, { key: '', value: '' }] })}
                    >
                      {t('form.addEntry')}
                    </Button>
                  </SpaceBetween>
                </FormField>
                <FormField label={t('form.instructions')}>
                  <Textarea
                    value={form.instructions}
                    onChange={({ detail }) => setForm({ ...form, instructions: detail.value })}
                    placeholder={t('form.instructionsPlaceholder')}
                    rows={10}
                  />
                </FormField>
              </SpaceBetween>
            </Form>
          </form>
        </Container>
      )}

      {!showForm && (
        <Table
          header={
            <Header
              variant="h2"
              description={t('toolbar.hint')}
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button iconName="refresh" onClick={load}>{t('toolbar.refresh')}</Button>
                  <Button variant="primary" onClick={handleCreate}>{t('toolbar.newSkill')}</Button>
                </SpaceBetween>
              }
            >
              {t('toolbar.title')}
            </Header>
          }
          loading={isLoading}
          loadingText={t('common.loading')}
          items={records}
          trackBy="recordId"
          columnDefinitions={[
            { id: 'name', header: t('table.name'), cell: (r) => r.name },
            { id: 'description', header: t('table.description'), cell: (r) => r.description },
            { id: 'status', header: t('table.status'), cell: (r) => renderStatus(r.status) },
            {
              id: 'updated',
              header: t('table.updated'),
              cell: (r) => (r.updatedAt ? new Date(r.updatedAt).toLocaleDateString() : '-'),
            },
            {
              id: 'actions',
              header: t('table.actions'),
              minWidth: 180,
              cell: (r) => (
                <SpaceBetween direction="horizontal" size="xs">
                  <Button onClick={() => handleEdit(r)}>{t('table.edit')}</Button>
                  <Button onClick={() => setDeleteTarget(r)}>{t('table.delete')}</Button>
                </SpaceBetween>
              ),
            },
          ]}
          empty={
            <Box textAlign="center" padding="m">
              <b>{t('table.empty')}</b>
              <Box variant="p" color="text-body-secondary" padding={{ top: 'xs' }}>
                {t('table.emptyHint')}
              </Box>
            </Box>
          }
        />
      )}
    </SpaceBetween>
  );
};

export default SkillManager;
