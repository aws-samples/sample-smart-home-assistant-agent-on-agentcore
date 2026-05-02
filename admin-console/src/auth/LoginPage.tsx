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
import { signIn, AuthTokens } from './CognitoAuth';
import { useI18n } from '../i18n';

interface LoginPageProps {
  onAuthenticated: (tokens: AuthTokens) => void;
}

const LoginPage: React.FC<LoginPageProps> = ({ onAuthenticated }) => {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const { t, language, setLanguage } = useI18n();

  const handleSignIn = async () => {
    setError('');
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

  return (
    <Box padding={{ top: 'xxxl' }}>
      <div style={{ maxWidth: 420, margin: '0 auto', padding: '0 16px' }}>
        <Box float="right" padding={{ bottom: 's' }}>
          <Button
            variant="link"
            onClick={() => setLanguage(language === 'en' ? 'zh' : 'en')}
          >
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
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void handleSignIn();
            }}
          >
            <Form
              actions={
                <Button
                  variant="primary"
                  formAction="submit"
                  loading={isLoading}
                  disabled={!email || !password}
                >
                  {isLoading ? t('login.signingIn') : t('login.signIn')}
                </Button>
              }
            >
              <SpaceBetween size="l" direction="vertical">
                {error && <Alert type="error">{error}</Alert>}
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
        </Container>
      </div>
    </Box>
  );
};

export default LoginPage;
