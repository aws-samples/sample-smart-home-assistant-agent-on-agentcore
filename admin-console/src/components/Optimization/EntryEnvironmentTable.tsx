import { useEffect, useState } from 'react';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import Table from '@cloudscape-design/components/table';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Modal from '@cloudscape-design/components/modal';
import FormField from '@cloudscape-design/components/form-field';
import Input from '@cloudscape-design/components/input';
import Select from '@cloudscape-design/components/select';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Alert from '@cloudscape-design/components/alert';
import { useI18n } from '../../i18n';
import {
  listTenantEnvs, putTenantEnv, deleteTenantEnv,
  TenantEnvOverride, EntryEnvironmentMode, PerUserPromptWillBeMaskedError,
} from '../../api/adminApi';

const MODE_OPTIONS: { value: EntryEnvironmentMode; labelKey: string }[] = [
  { value: 'default', labelKey: 'optimization.tenantEnv.modeDefault' },
  { value: 'ab-bundles', labelKey: 'optimization.tenantEnv.modeAbBundles' },
  { value: 'ab-targets', labelKey: 'optimization.tenantEnv.modeAbTargets' },
];

function modeLabelKey(m: EntryEnvironmentMode): string {
  return m === 'default' ? 'optimization.tenantEnv.modeDefault'
    : m === 'ab-bundles' ? 'optimization.tenantEnv.modeAbBundles'
    : 'optimization.tenantEnv.modeAbTargets';
}

export function EntryEnvironmentTable() {
  const { t } = useI18n();
  const [items, setItems] = useState<TenantEnvOverride[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [modalOpen, setModalOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<TenantEnvOverride | null>(null);
  const [emailInput, setEmailInput] = useState('');
  const [modeInput, setModeInput] = useState<EntryEnvironmentMode>('default');
  const [saving, setSaving] = useState(false);
  const [maskConfirmOpen, setMaskConfirmOpen] = useState<string | null>(null);

  const refresh = async () => {
    setLoading(true);
    try {
      setItems(await listTenantEnvs());
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'failed');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { refresh(); }, []);

  const openAdd = () => {
    setEditTarget(null);
    setEmailInput('');
    setModeInput('default');
    setModalOpen(true);
  };

  const openEdit = (row: TenantEnvOverride) => {
    setEditTarget(row);
    setEmailInput(row.email);
    setModeInput(row.mode);
    setModalOpen(true);
  };

  const submit = async (acknowledgeMask = false) => {
    setSaving(true);
    try {
      await putTenantEnv(emailInput, modeInput, acknowledgeMask);
      setModalOpen(false);
      setMaskConfirmOpen(null);
      await refresh();
    } catch (e: unknown) {
      if (e instanceof PerUserPromptWillBeMaskedError) {
        setMaskConfirmOpen(e.email);
        return;
      }
      setError(e instanceof Error ? e.message : 'failed');
    } finally {
      setSaving(false);
    }
  };

  const remove = async (email: string) => {
    await deleteTenantEnv(email);
    await refresh();
  };

  return (
    <Container
      header={
        <Header variant="h2" description={t('optimization.tenantEnv.description')}>
          {t('optimization.tenantEnv.sectionTitle')}
        </Header>
      }
    >
      {error && <Alert type="error">{error}</Alert>}
      <Box variant="p">
        {t('optimization.tenantEnv.defaultLabel')}{' '}
        <strong>{t('optimization.tenantEnv.defaultValueRuntime')}</strong>
      </Box>
      <Box margin={{ top: 's' }}>{t('optimization.tenantEnv.propagationNote')}</Box>
      <Box margin={{ top: 'm' }}>
        <Button onClick={openAdd}>{t('optimization.tenantEnv.addOverride')}</Button>
      </Box>
      <Table
        loading={loading}
        items={items}
        columnDefinitions={[
          { id: 'email', header: t('optimization.tenantEnv.columnEmail'),
            cell: (i: TenantEnvOverride) => i.email },
          { id: 'mode', header: t('optimization.tenantEnv.columnMode'),
            cell: (i: TenantEnvOverride) => t(modeLabelKey(i.mode)) },
          { id: 'updatedBy', header: t('optimization.tenantEnv.columnUpdatedBy'),
            cell: (i: TenantEnvOverride) => i.updatedBy || '—' },
          { id: 'actions', header: t('optimization.tenantEnv.columnActions'),
            cell: (i: TenantEnvOverride) => (
              <SpaceBetween size="xs" direction="horizontal">
                <Button variant="inline-link" onClick={() => openEdit(i)}>
                  {t('optimization.tenantEnv.edit')}
                </Button>
                <Button variant="inline-link" onClick={() => remove(i.email)}>
                  {t('optimization.tenantEnv.remove')}
                </Button>
              </SpaceBetween>
            ) },
        ]}
      />

      <Modal
        visible={modalOpen}
        onDismiss={() => setModalOpen(false)}
        header={editTarget
          ? t('optimization.tenantEnv.modalEditTitle')
          : t('optimization.tenantEnv.modalAddTitle')}
        footer={
          <Box float="right">
            <SpaceBetween size="xs" direction="horizontal">
              <Button onClick={() => setModalOpen(false)}>
                {t('optimization.tenantEnv.modalCancel')}
              </Button>
              <Button variant="primary" loading={saving} onClick={() => submit(false)}>
                {t('optimization.tenantEnv.modalSave')}
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <FormField label={t('optimization.tenantEnv.modalEmailLabel')}>
          <Input
            value={emailInput}
            onChange={({ detail }) => setEmailInput(detail.value)}
            disabled={!!editTarget}
          />
        </FormField>
        <FormField label={t('optimization.tenantEnv.modalModeLabel')}>
          <Select
            selectedOption={{ value: modeInput, label: t(modeLabelKey(modeInput)) }}
            options={MODE_OPTIONS.map(o => ({ value: o.value, label: t(o.labelKey) }))}
            onChange={({ detail }) =>
              setModeInput(detail.selectedOption.value as EntryEnvironmentMode)}
          />
        </FormField>
      </Modal>

      {maskConfirmOpen && (
        <Modal
          visible={true}
          onDismiss={() => setMaskConfirmOpen(null)}
          header={t('optimization.tenantEnv.confirmMaskTitle')}
          footer={
            <Box float="right">
              <SpaceBetween size="xs" direction="horizontal">
                <Button onClick={() => setMaskConfirmOpen(null)}>
                  {t('optimization.tenantEnv.modalCancel')}
                </Button>
                <Button variant="primary" loading={saving}
                        onClick={() => submit(true)}>
                  {t('optimization.tenantEnv.confirmMaskConfirm')}
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <Alert type="warning">
            {t('optimization.tenantEnv.confirmMaskBody')}
          </Alert>
        </Modal>
      )}
    </Container>
  );
}
