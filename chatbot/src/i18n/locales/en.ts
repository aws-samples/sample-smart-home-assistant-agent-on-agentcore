const en: Record<string, string> = {
  // App
  'app.loading': 'Loading...',
  'app.title': 'Smart Home Assistant',
  'app.signOut': 'Sign Out',

  // Login
  'login.title': 'Smart Home Assistant',
  'login.signIn.subtitle': 'Sign in to your account',
  'login.signUp.subtitle': 'Create a new account',
  'login.confirm.subtitle': 'Verify your email',
  'login.username': 'Username',
  'login.email': 'Email',
  'login.password': 'Password',
  'login.confirmCode': 'Confirmation Code',
  'login.enterUsername': 'Enter your username',
  'login.enterPassword': 'Enter your password',
  'login.chooseUsername': 'Choose a username',
  'login.enterEmail': 'Enter your email',
  'login.choosePassword': 'Choose a password',
  'login.enterCode': 'Enter the 6-digit code',
  'login.signingIn': 'Signing in...',
  'login.signIn': 'Sign In',
  'login.creatingAccount': 'Creating account...',
  'login.signUp': 'Sign Up',
  'login.verifying': 'Verifying...',
  'login.confirmAccount': 'Confirm Account',
  'login.noAccount': "Don't have an account?",
  'login.hasAccount': 'Already have an account?',
  'login.backToSignIn': 'Back to Sign In',
  'login.verificationSent': 'A verification code has been sent to your email address.',
  'login.signInFailed': 'Sign in failed',
  'login.signUpFailed': 'Sign up failed',
  'login.confirmFailed': 'Confirmation failed',

  // Chat
  'chat.placeholder': 'Type a message...',
  'chat.welcome': 'Welcome to Smart Home Assistant',
  'chat.subtitle': 'Control your smart home devices with natural language.',
  // Example library. The example TEXT lives in shared/prompt-examples.json, not
  // here: scripts/sim/personas.py reads the same file to generate demo traffic,
  // and Python cannot read a .ts module. Only the shell is translated.
  'examples.title': 'Example prompts',
  'examples.subtitle': '{count} examples covering every capability',
  'examples.search': 'Search examples',
  'examples.noMatch': 'No example matches that search',
  'examples.close': 'Close examples',
  'examples.heavy': 'slow',
  'examples.openAll': 'Browse all examples',

  // Per-turn feedback
  'chat.feedback.up': 'This was helpful',
  'chat.feedback.down': 'This was not helpful',
  'chat.feedback.thanks': 'Thanks for the feedback',
  'chat.feedback.reasonPlaceholder': 'What went wrong? (optional)',
  'chat.feedback.reasonSubmit': 'Send',
  'chat.feedback.reasonSkip': 'Skip',
  'chat.feedback.failed': 'Could not record your feedback. Please try again.',

  // Suggestion group headings

  // Knowledge base chips (answered from enterprise KB documents)

  // Weather chips (weather-lookup skill via http_request)

  // Live browser chips (browser-use skill \u2192 browse_web tool)

  // Code interpreter chips (code-interpreter skill \u2192 execute_python tool)

  // Vision chip \u2014 reminds users they can attach an image via the paperclip
  'chat.trace': "how this was answered",
  'chat.typing': 'thinking…',
  'chat.consulting': 'asking the',
  'chat.sendFailed': 'Failed to send message',
  'chat.voiceMode.enter': 'Switch to voice mode',
  'chat.voiceMode.exit': 'Switch to text mode',
  'chat.voiceMode.connecting': 'Connecting to voice assistant...',
  'chat.voiceMode.connected': 'Listening. Speak now.',
  'chat.voiceMode.disconnected': 'Voice session ended.',
  'chat.voiceMode.micDenied': 'Microphone access denied.',
  'chat.voiceMode.error': 'Voice session error',
  'chat.attachImage': 'Attach image',
  'chat.image.tooBig': '{name} — exceeds 20 MB',
  'chat.image.badFormat': '{name} — unsupported format',
  'chat.image.tooMany': 'Maximum 3 images per message',
  'chat.image.readFailed': '{name} — could not be read',
  'chat.image.remove': 'Remove image',

  // Browser panel
  'browserPanel.title': 'Browser',
  'browserPanel.liveView': 'Live view',
  'browserPanel.files': 'Files',
  'browserPanel.noActive': 'No browser session is running.',
  'browserPanel.opening': 'Opening browser...',
  'browserPanel.goal': 'Goal',
  'browserPanel.refresh': 'Refresh',
  'browserPanel.download': 'Download',
  'browserPanel.parent': 'Up',
  'browserPanel.errorLoad': 'Could not load files.',
  'browserPanel.emptyDir': 'Empty.',
  'browserPanel.sessionEnded': 'Browser session ended. Files from the run are still available in the Files tab.',
  'browserPanel.takeControl': 'Take control',
  'browserPanel.releaseControl': 'Release control to agent',
  'browserPanel.humanMode': 'Human control — agent paused.',
  'browserPanel.idleMode': 'Agent done — browser still yours until it times out.',
  'browserPanel.collapse': 'Collapse',
  'browserPanel.maximize': 'Maximize',
  'browserPanel.restore': 'Restore',

  // Code Interpreter panel
  'codePanel.title': 'CodeInterpreter',
  'codePanel.task': 'Task',
  'codePanel.step': 'Step',
  'codePanel.running': 'Running',
  'codePanel.done': 'Done',
  'codePanel.failed': 'Failed',
  'codePanel.executing': 'Executing…',
  'codePanel.noActive': 'No code is running. Try a Code interpreter example to see live execution here.',
};

export default en;
