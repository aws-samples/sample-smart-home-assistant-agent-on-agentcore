import React, { useState } from 'react';
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

interface LoginPageProps {
  onAuthenticated: (tokens: AuthTokens) => void;
}

type Mode = 'signin' | 'signup' | 'confirm';

const LoginPage: React.FC<LoginPageProps> = ({ onAuthenticated }) => {
  const [mode, setMode] = useState<Mode>('signin');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [code, setCode] = useState('');
  const [error, setError] = useState('');
  const [info, setInfo] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const { t, language, setLanguage } = useI18n();

  const clearMessages = () => {
    setError('');
    setInfo('');
  };

  const handleSignIn = async () => {
    clearMessages();
    setIsLoading(true);
    try {
      const tokens = await signIn(email, password);
      onAuthenticated(tokens);
    } catch (err: any) {
      setError(err.message || t('login.signInFailed'));
    } finally {
      setIsLoading(false);
    }
  };

  const handleSignUp = async () => {
    clearMessages();
    setIsLoading(true);
    try {
      await signUp(email, password);
      setMode('confirm');
      setInfo(t('login.confirmSent'));
    } catch (err: any) {
      setError(err.message || t('login.signUpFailed'));
    } finally {
      setIsLoading(false);
    }
  };

  const handleConfirm = async () => {
    clearMessages();
    setIsLoading(true);
    try {
      await confirmSignUp(email, code);
      const tokens = await signIn(email, password);
      onAuthenticated(tokens);
    } catch (err: any) {
      setError(err.message || t('login.confirmFailed'));
    } finally {
      setIsLoading(false);
    }
  };

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
            <Header variant="h1" description={t('login.subtitle')}>
              {t('login.title')}
            </Header>
          }
        >
          {mode === 'signin' && (
            <form onSubmit={(e) => { e.preventDefault(); void handleSignIn(); }}>
              <Form
                actions={
                  <SpaceBetween direction="horizontal" size="xs">
                    <Button variant="link" onClick={() => { clearMessages(); setMode('signup'); }}>
                      {t('login.noAccount')}
                    </Button>
                    <Button variant="primary" formAction="submit" loading={isLoading} disabled={!email || !password}>
                      {t('login.signIn')}
                    </Button>
                  </SpaceBetween>
                }
              >
                <SpaceBetween size="l">
                  {error && <Alert type="error">{error}</Alert>}
                  {info && <Alert type="info">{info}</Alert>}
                  <FormField label={t('login.email')}>
                    <Input type="email" value={email} onChange={({ detail }) => setEmail(detail.value)} />
                  </FormField>
                  <FormField label={t('login.password')}>
                    <Input type="password" value={password} onChange={({ detail }) => setPassword(detail.value)} />
                  </FormField>
                </SpaceBetween>
              </Form>
            </form>
          )}

          {mode === 'signup' && (
            <form onSubmit={(e) => { e.preventDefault(); void handleSignUp(); }}>
              <Form
                actions={
                  <SpaceBetween direction="horizontal" size="xs">
                    <Button variant="link" onClick={() => { clearMessages(); setMode('signin'); }}>
                      {t('login.haveAccount')}
                    </Button>
                    <Button variant="primary" formAction="submit" loading={isLoading} disabled={!email || !password}>
                      {t('login.signUp')}
                    </Button>
                  </SpaceBetween>
                }
              >
                <SpaceBetween size="l">
                  {error && <Alert type="error">{error}</Alert>}
                  <FormField label={t('login.email')}>
                    <Input type="email" value={email} onChange={({ detail }) => setEmail(detail.value)} />
                  </FormField>
                  <FormField label={t('login.password')}>
                    <Input type="password" value={password} onChange={({ detail }) => setPassword(detail.value)} />
                  </FormField>
                </SpaceBetween>
              </Form>
            </form>
          )}

          {mode === 'confirm' && (
            <form onSubmit={(e) => { e.preventDefault(); void handleConfirm(); }}>
              <Form
                actions={
                  <Button variant="primary" formAction="submit" loading={isLoading} disabled={!code}>
                    {t('login.confirm')}
                  </Button>
                }
              >
                <SpaceBetween size="l">
                  {error && <Alert type="error">{error}</Alert>}
                  {info && <Alert type="info">{info}</Alert>}
                  <FormField label={t('login.confirmCode')}>
                    <Input value={code} onChange={({ detail }) => setCode(detail.value)} />
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
