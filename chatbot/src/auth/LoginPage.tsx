import React, { useState, useEffect } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Container from '@cloudscape-design/components/container';
import Form from '@cloudscape-design/components/form';
import FormField from '@cloudscape-design/components/form-field';
import Header from '@cloudscape-design/components/header';
import Input from '@cloudscape-design/components/input';
import SpaceBetween from '@cloudscape-design/components/space-between';
import { signIn, signUp, confirmSignUp, AuthTokens } from './CognitoAuth';
import { useI18n } from '../i18n';
import { getConfig } from '../config';

interface LoginPageProps {
  onAuthenticated: (tokens: AuthTokens) => void;
}

type FormMode = 'signIn' | 'signUp' | 'confirm';

function getInitialUsername(): string {
  if (typeof window === 'undefined') return '';
  try {
    const params = new URLSearchParams(window.location.search);
    return params.get('username') || params.get('email') || '';
  } catch {
    return '';
  }
}

const LoginPage: React.FC<LoginPageProps> = ({ onAuthenticated }) => {
  const [mode, setMode] = useState<FormMode>('signIn');
  const [username, setUsername] = useState(getInitialUsername);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirmCode, setConfirmCode] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const { t, language, setLanguage } = useI18n();

  // Preconnect to the AgentCore Runtime endpoint while the user types their
  // password so the TLS handshake for the post-login warmup + the voice WS
  // presign is already paid by the time we need it.
  useEffect(() => {
    try {
      const region = getConfig().region;
      const href = `https://bedrock-agentcore.${region}.amazonaws.com`;
      const link = document.createElement('link');
      link.rel = 'preconnect';
      link.href = href;
      link.crossOrigin = 'anonymous';
      document.head.appendChild(link);
      return () => {
        document.head.removeChild(link);
      };
    } catch {
      // Non-critical hint; ignore failures.
    }
  }, []);

  const handleSignIn = async () => {
    setError('');
    setLoading(true);
    try {
      const tokens = await signIn(username, password);
      onAuthenticated(tokens);
    } catch (err: any) {
      setError(err.message || t('login.signInFailed'));
    } finally {
      setLoading(false);
    }
  };

  const handleSignUp = async () => {
    setError('');
    setLoading(true);
    try {
      await signUp(username, password, email);
      setMode('confirm');
    } catch (err: any) {
      setError(err.message || t('login.signUpFailed'));
    } finally {
      setLoading(false);
    }
  };

  const handleConfirm = async () => {
    setError('');
    setLoading(true);
    try {
      await confirmSignUp(username, confirmCode);
      const tokens = await signIn(username, password);
      onAuthenticated(tokens);
    } catch (err: any) {
      setError(err.message || t('login.confirmFailed'));
    } finally {
      setLoading(false);
    }
  };

  const subtitle =
    mode === 'signIn' ? t('login.signIn.subtitle')
    : mode === 'signUp' ? t('login.signUp.subtitle')
    : t('login.confirm.subtitle');

  return (
    <Box padding={{ top: 'xxxl' }}>
      <div style={{ maxWidth: 420, margin: '0 auto', padding: '0 16px' }}>
        <Box float="right" padding={{ bottom: 's' }}>
          <Button variant="link" onClick={() => setLanguage(language === 'en' ? 'zh' : 'en')}>
            {language === 'en' ? '中文' : 'EN'}
          </Button>
        </Box>
        <Container
          header={
            <Header variant="h1" description={subtitle}>
              {t('login.title')}
            </Header>
          }
        >
          {mode === 'signIn' && (
            <form onSubmit={(e) => { e.preventDefault(); void handleSignIn(); }}>
              <Form
                actions={
                  <SpaceBetween direction="horizontal" size="xs">
                    <Button variant="link" onClick={() => { setMode('signUp'); setError(''); }}>
                      {t('login.noAccount')} {t('login.signUp')}
                    </Button>
                    <Button variant="primary" formAction="submit" loading={loading} disabled={!username || !password}>
                      {t('login.signIn')}
                    </Button>
                  </SpaceBetween>
                }
              >
                <SpaceBetween size="l">
                  {error && <Alert type="error">{error}</Alert>}
                  <FormField label={t('login.username')}>
                    <Input
                      value={username}
                      onChange={({ detail }) => setUsername(detail.value)}
                      placeholder={t('login.enterUsername')}
                    />
                  </FormField>
                  <FormField label={t('login.password')}>
                    <Input
                      type="password"
                      value={password}
                      onChange={({ detail }) => setPassword(detail.value)}
                      placeholder={t('login.enterPassword')}
                    />
                  </FormField>
                </SpaceBetween>
              </Form>
            </form>
          )}

          {mode === 'signUp' && (
            <form onSubmit={(e) => { e.preventDefault(); void handleSignUp(); }}>
              <Form
                actions={
                  <SpaceBetween direction="horizontal" size="xs">
                    <Button variant="link" onClick={() => { setMode('signIn'); setError(''); }}>
                      {t('login.hasAccount')} {t('login.signIn')}
                    </Button>
                    <Button variant="primary" formAction="submit" loading={loading} disabled={!username || !email || !password}>
                      {t('login.signUp')}
                    </Button>
                  </SpaceBetween>
                }
              >
                <SpaceBetween size="l">
                  {error && <Alert type="error">{error}</Alert>}
                  <FormField label={t('login.username')}>
                    <Input
                      value={username}
                      onChange={({ detail }) => setUsername(detail.value)}
                      placeholder={t('login.chooseUsername')}
                    />
                  </FormField>
                  <FormField label={t('login.email')}>
                    <Input
                      type="email"
                      value={email}
                      onChange={({ detail }) => setEmail(detail.value)}
                      placeholder={t('login.enterEmail')}
                    />
                  </FormField>
                  <FormField label={t('login.password')}>
                    <Input
                      type="password"
                      value={password}
                      onChange={({ detail }) => setPassword(detail.value)}
                      placeholder={t('login.choosePassword')}
                    />
                  </FormField>
                </SpaceBetween>
              </Form>
            </form>
          )}

          {mode === 'confirm' && (
            <form onSubmit={(e) => { e.preventDefault(); void handleConfirm(); }}>
              <Form
                actions={
                  <Button variant="primary" formAction="submit" loading={loading} disabled={!confirmCode}>
                    {t('login.confirm')}
                  </Button>
                }
              >
                <SpaceBetween size="l">
                  {error && <Alert type="error">{error}</Alert>}
                  <Alert type="info">{t('login.verificationSent')}</Alert>
                  <FormField label={t('login.confirmCode')}>
                    <Input
                      value={confirmCode}
                      onChange={({ detail }) => setConfirmCode(detail.value)}
                      placeholder={t('login.enterCode')}
                    />
                  </FormField>
                </SpaceBetween>
              </Form>
            </form>
          )}
        </Container>
      </div>
    </Box>
  );
};

export default LoginPage;
