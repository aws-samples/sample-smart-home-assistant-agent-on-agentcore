import React, { useState, useEffect, useCallback } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Badge from '@cloudscape-design/components/badge';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Checkbox from '@cloudscape-design/components/checkbox';
import Container from '@cloudscape-design/components/container';
import Form from '@cloudscape-design/components/form';
import FormField from '@cloudscape-design/components/form-field';
import Header from '@cloudscape-design/components/header';
import Input from '@cloudscape-design/components/input';
import Modal from '@cloudscape-design/components/modal';
import Select from '@cloudscape-design/components/select';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Table from '@cloudscape-design/components/table';
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

  const onSubmit = async () => {
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

  const renderStatus = (status: string) => {
    const label = statusLabel(status);
    const s = status.toUpperCase();
    if (s === 'APPROVED' || s === 'PUBLISHED') return <StatusIndicator type="success">{label}</StatusIndicator>;
    if (s === 'PENDING' || s === 'REVIEW') return <StatusIndicator type="pending">{label}</StatusIndicator>;
    if (s === 'REJECTED') return <StatusIndicator type="error">{label}</StatusIndicator>;
    return <Badge>{label}</Badge>;
  };

  const updateSkill = (idx: number, patch: Partial<A2ASkill>) => {
    const next = form.skills.map((s, i) => (i === idx ? { ...s, ...patch } : s));
    setForm({ ...form, skills: next });
  };

  const authOptions = [
    { value: 'none', label: 'none' },
    { value: 'bearer', label: 'bearer' },
    { value: 'apiKey', label: 'apiKey' },
  ];
  const selectedAuth = authOptions.find((o) => o.value === form.auth) ?? authOptions[0];

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
                <Button variant="primary" onClick={onDelete}>{t('table.delete')}</Button>
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
              {editingId ? t('erp.a2a.form.editTitle') : t('erp.a2a.form.createTitle')}
            </Header>
          }
        >
          <form onSubmit={(e) => { e.preventDefault(); void onSubmit(); }}>
            <Form
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button onClick={cancel}>{t('form.cancel')}</Button>
                  <Button variant="primary" formAction="submit">
                    {editingId ? t('form.save') : t('form.create')}
                  </Button>
                </SpaceBetween>
              }
            >
              <SpaceBetween size="m">
                <FormField
                  label={<>{t('erp.a2a.form.name')} <span style={{ color: '#d91515' }}>*</span></>}
                  description={t('erp.a2a.form.nameHint')}
                >
                  <Input
                    value={form.name}
                    onChange={({ detail }) => setForm({ ...form, name: detail.value.toLowerCase() })}
                    disabled={!!editingId}
                    placeholder="weather-advisor-agent"
                  />
                </FormField>
                <FormField label={<>{t('erp.a2a.form.description')} <span style={{ color: '#d91515' }}>*</span></>}>
                  <Input
                    value={form.description}
                    onChange={({ detail }) => setForm({ ...form, description: detail.value })}
                  />
                </FormField>
                <FormField label={<>{t('erp.a2a.form.endpoint')} <span style={{ color: '#d91515' }}>*</span></>}>
                  <Input
                    value={form.endpoint}
                    onChange={({ detail }) => setForm({ ...form, endpoint: detail.value })}
                    placeholder="https://example.com/a2a/..."
                  />
                </FormField>
                <FormField label={<>{t('erp.a2a.form.version')} <span style={{ color: '#d91515' }}>*</span></>}>
                  <Input
                    value={form.version}
                    onChange={({ detail }) => setForm({ ...form, version: detail.value })}
                    placeholder="1.0.0"
                  />
                </FormField>
                <FormField label={t('erp.a2a.form.provider')}>
                  <Input
                    value={form.provider}
                    onChange={({ detail }) => setForm({ ...form, provider: detail.value })}
                  />
                </FormField>
                <FormField label={<>{t('erp.a2a.form.auth')} <span style={{ color: '#d91515' }}>*</span></>}>
                  <Select
                    selectedOption={selectedAuth}
                    onChange={({ detail }) => setForm({ ...form, auth: detail.selectedOption.value as any })}
                    options={authOptions}
                  />
                </FormField>
                <FormField label={t('erp.a2a.form.capabilities')}>
                  <SpaceBetween direction="horizontal" size="m">
                    {(['streaming', 'pushNotifications', 'stateTransitionHistory'] as const).map((k) => (
                      <Checkbox
                        key={k}
                        checked={form.capabilities[k]}
                        onChange={({ detail }) => setForm({
                          ...form,
                          capabilities: { ...form.capabilities, [k]: detail.checked },
                        })}
                      >
                        {k}
                      </Checkbox>
                    ))}
                  </SpaceBetween>
                </FormField>
                <FormField label={t('erp.a2a.form.tags')} description={t('erp.a2a.form.tagsHint')}>
                  <Input
                    value={tagsInput}
                    onChange={({ detail }) => setTagsInput(detail.value)}
                    placeholder="energy, demo, smarthome"
                  />
                </FormField>
                <Box color="text-body-secondary" fontSize="body-s">{t('erp.a2a.form.skillsSection')}</Box>
                {form.skills.map((s, i) => (
                  <Container key={i}>
                    <SpaceBetween size="s">
                      <FormField label={t('erp.a2a.form.skillId')}>
                        <Input
                          value={s.id}
                          onChange={({ detail }) => updateSkill(i, { id: detail.value.toLowerCase() })}
                        />
                      </FormField>
                      <FormField label={t('erp.a2a.form.skillName')}>
                        <Input
                          value={s.name}
                          onChange={({ detail }) => updateSkill(i, { name: detail.value })}
                        />
                      </FormField>
                      <FormField label={t('erp.a2a.form.skillDescription')}>
                        <Input
                          value={s.description}
                          onChange={({ detail }) => updateSkill(i, { description: detail.value })}
                        />
                      </FormField>
                      <FormField label={t('erp.a2a.form.skillExamples')} description={t('erp.a2a.form.skillExamplesHint')}>
                        <Input
                          value={(s.examples || []).join(' | ')}
                          onChange={({ detail }) => updateSkill(i, {
                            examples: detail.value.split('|').map((x) => x.trim()).filter(Boolean),
                          })}
                          placeholder="first example | second example"
                        />
                      </FormField>
                      {form.skills.length > 1 && (
                        <Button
                          onClick={() => setForm({ ...form, skills: form.skills.filter((_, k) => k !== i) })}
                        >
                          {t('erp.a2a.form.removeSkill')}
                        </Button>
                      )}
                    </SpaceBetween>
                  </Container>
                ))}
                <Button onClick={() => setForm({ ...form, skills: [...form.skills, emptySkill()] })}>
                  {t('erp.a2a.form.addSkill')}
                </Button>
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
              description={t('erp.a2a.list.hint')}
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button iconName="refresh" onClick={load}>{t('toolbar.refresh')}</Button>
                  <Button variant="primary" onClick={startCreate}>{t('erp.a2a.list.create')}</Button>
                </SpaceBetween>
              }
            >
              {t('erp.a2a.list.title')}
            </Header>
          }
          loading={isLoading}
          loadingText={t('common.loading')}
          items={records}
          trackBy="recordId"
          columnDefinitions={[
            { id: 'name', header: t('table.name'), cell: (r) => r.name },
            { id: 'description', header: t('table.description'), cell: (r) => r.description },
            { id: 'endpoint', header: t('erp.a2a.list.colEndpoint'), cell: (r) => r.card.endpoint },
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
                  <Button onClick={() => startEdit(r)}>{t('table.edit')}</Button>
                  <Button onClick={() => setDeleteTarget(r)}>{t('table.delete')}</Button>
                </SpaceBetween>
              ),
            },
          ]}
          empty={
            <Box textAlign="center" padding="m">
              <b>{t('erp.a2a.list.empty')}</b>
              <Box variant="p" color="text-body-secondary" padding={{ top: 'xs' }}>
                {t('erp.a2a.list.emptyHint')}
              </Box>
            </Box>
          }
        />
      )}
    </SpaceBetween>
  );
};

export default A2AAgentsTab;
