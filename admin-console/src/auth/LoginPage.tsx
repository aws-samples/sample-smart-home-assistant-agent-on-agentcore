import React, { useState } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Container from '@cloudscape-design/components/container';
import Form from '@cloudscape-design/components/form';
import FormField from '@cloudscape-design/components/form-field';
import Header from '@cloudscape-design/components/header';
import Input from '@cloudscape-design/components/input';
import Link from '@cloudscape-design/components/link';
import SpaceBetween from '@cloudscape-design/components/space-between';
import { signIn, signUp, confirmSignUp, resendConfirmationCode, AuthTokens } from './CognitoAuth';
import { getConfig } from '../config';
import { useI18n } from '../i18n';

interface LoginPageProps {
  onAuthenticated: (tokens: AuthTokens) => void;
}

type Mode = 'signIn' | 'signUp' | 'confirm';

const LoginPage: React.FC<LoginPageProps> = ({ onAuthenticated }) => {
  const [mode, setMode] = useState<Mode>('signIn');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [code, setCode] = useState('');
  const [error, setError] = useState('');
  const [info, setInfo] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const { t, language, setLanguage } = useI18n();

  const chatbotUrl = getConfig().chatbotUrl;

  const switchMode = (next: Mode) => {
    setMode(next);
    setError('');
    setInfo('');
  };

  const run = async (fn: () => Promise<void>) => {
    setError('');
    setIsLoading(true);
    try {
      await fn();
    } catch (err: any) {
      setError(err.message || t('login.signInFailed'));
    } finally {
      setIsLoading(false);
    }
  };

  const handleSignIn = () =>
    run(async () => {
      const tokens = await signIn(email, password);
      onAuthenticated(tokens);
    });

  const handleSignUp = () =>
    run(async () => {
      // Email is the username (pool uses UsernameAttributes: ["email"]).
      await signUp(email, password);
      setInfo(t('login.confirm.sent').replace('{email}', email));
      setMode('confirm');
    });

  const handleConfirm = () =>
    run(async () => {
      await confirmSignUp(email, code);
      // Confirmed accounts can sign in immediately, but stay non-admin until an
      // administrator grants the group — so land them on the sign-in form with
      // that expectation set rather than auto-signing them in.
      setInfo(t('login.confirm.done'));
      setCode('');
      setPassword('');
      setMode('signIn');
    });

  const handleResend = () =>
    run(async () => {
      await resendConfirmationCode(email);
      setInfo(t('login.confirm.sent').replace('{email}', email));
    });

  const subtitle =
    mode === 'signIn' ? t('login.subtitle')
      : mode === 'signUp' ? t('login.signUp.subtitle')
        : t('login.confirm.subtitle');

  /**
   * Shown on the sign-up and confirm forms: a new account can authenticate but
   * this console is admin-gated, so without the `admin` group the only usable
   * surface is the chatbot. Saying it here prevents a confusing "Access Denied"
   * as the first thing a new user sees.
   */
  const adminNotice = (
    <Alert type="info" header={t('login.adminNotice.header')}>
      <SpaceBetween size="xs">
        <span>{t('login.adminNotice.body')}</span>
        {chatbotUrl && (
          <Link href={chatbotUrl} external externalIconAriaLabel={t('login.adminNotice.opensNewTab')}>
            {t('login.adminNotice.openChatbot')}
          </Link>
        )}
      </SpaceBetween>
    </Alert>
  );

  const banners = (
    <>
      {error && <Alert type="error">{error}</Alert>}
      {info && <Alert type="success">{info}</Alert>}
    </>
  );

  return (
    <Box padding={{ top: 'xxxl' }}>
      <div style={{ maxWidth: 460, margin: '0 auto', padding: '0 16px' }}>
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
                    <Button variant="link" formAction="none" onClick={() => switchMode('signUp')}>
                      {t('login.noAccount')}
                    </Button>
                    <Button
                      variant="primary"
                      formAction="submit"
                      loading={isLoading}
                      disabled={!email || !password}
                    >
                      {isLoading ? t('login.signingIn') : t('login.signIn')}
                    </Button>
                  </SpaceBetween>
                }
              >
                <SpaceBetween size="l">
                  {banners}
                  <FormField label={t('login.email')}>
                    <Input
                      type="email"
                      value={email}
                      onChange={({ detail }) => setEmail(detail.value)}
                      placeholder={t('login.emailPlaceholder')}
                    />
                  </FormField>
                  <FormField label={t('login.password')}>
                    <Input
                      type="password"
                      value={password}
                      onChange={({ detail }) => setPassword(detail.value)}
                      placeholder={t('login.passwordPlaceholder')}
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
                    <Button variant="link" formAction="none" onClick={() => switchMode('signIn')}>
                      {t('login.hasAccount')}
                    </Button>
                    <Button
                      variant="primary"
                      formAction="submit"
                      loading={isLoading}
                      disabled={!email || !password}
                    >
                      {t('login.signUp')}
                    </Button>
                  </SpaceBetween>
                }
              >
                <SpaceBetween size="l">
                  {banners}
                  {adminNotice}
                  <FormField label={t('login.email')}>
                    <Input
                      type="email"
                      value={email}
                      onChange={({ detail }) => setEmail(detail.value)}
                      placeholder={t('login.emailPlaceholder')}
                    />
                  </FormField>
                  <FormField
                    label={t('login.password')}
                    description={t('login.passwordPolicy')}
                  >
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
                  <SpaceBetween direction="horizontal" size="xs">
                    <Button variant="link" formAction="none" onClick={() => switchMode('signIn')}>
                      {t('login.backToSignIn')}
                    </Button>
                    <Button variant="normal" formAction="none" onClick={() => void handleResend()} disabled={isLoading || !email}>
                      {t('login.confirm.resend')}
                    </Button>
                    <Button
                      variant="primary"
                      formAction="submit"
                      loading={isLoading}
                      disabled={!email || !code}
                    >
                      {t('login.confirm.submit')}
                    </Button>
                  </SpaceBetween>
                }
              >
                <SpaceBetween size="l">
                  {banners}
                  {adminNotice}
                  <FormField label={t('login.email')}>
                    <Input
                      type="email"
                      value={email}
                      onChange={({ detail }) => setEmail(detail.value)}
                      placeholder={t('login.emailPlaceholder')}
                    />
                  </FormField>
                  <FormField label={t('login.confirm.code')}>
                    <Input
                      value={code}
                      onChange={({ detail }) => setCode(detail.value)}
                      placeholder={t('login.confirm.codePlaceholder')}
                      inputMode="numeric"
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
