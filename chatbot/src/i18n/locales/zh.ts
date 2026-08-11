const zh: Record<string, string> = {
  // App
  'app.loading': '加载中...',
  'app.title': '智能家居助手',
  'app.signOut': '退出登录',

  // Login
  'login.title': '智能家居助手',
  'login.signIn.subtitle': '登录您的账户',
  'login.signUp.subtitle': '创建新账户',
  'login.confirm.subtitle': '验证您的邮箱',
  'login.username': '用户名',
  'login.email': '邮箱',
  'login.password': '密码',
  'login.confirmCode': '验证码',
  'login.enterUsername': '请输入用户名',
  'login.enterPassword': '请输入密码',
  'login.chooseUsername': '请选择用户名',
  'login.enterEmail': '请输入邮箱',
  'login.choosePassword': '请选择密码',
  'login.enterCode': '请输入6位验证码',
  'login.signingIn': '登录中...',
  'login.signIn': '登录',
  'login.creatingAccount': '创建账户中...',
  'login.signUp': '注册',
  'login.verifying': '验证中...',
  'login.confirmAccount': '确认账户',
  'login.noAccount': '还没有账户？',
  'login.hasAccount': '已有账户？',
  'login.backToSignIn': '返回登录',
  'login.verificationSent': '验证码已发送到您的邮箱地址。',
  'login.signInFailed': '登录失败',
  'login.signUpFailed': '注册失败',
  'login.confirmFailed': '验证失败',

  // Chat
  'chat.placeholder': '输入消息...',
  'chat.welcome': '欢迎使用智能家居助手',
  'chat.subtitle': '用自然语言控制您的智能家居设备。',
  // 示例库。示例正文在 shared/prompt-examples.json，不在这里 —— sim 侧的
  // personas.py 要读同一份文件生成演示流量，而 Python 读不了 .ts。
  // 这里只翻译外壳文案。
  'examples.title': '示例提示词',
  'examples.subtitle': '{count} 条示例，覆盖全部能力',
  'examples.search': '搜索示例',
  'examples.noMatch': '没有匹配的示例',
  'examples.close': '关闭示例',
  'examples.heavy': '较慢',
  'examples.openAll': '浏览全部示例',

  // 单轮反馈
  'chat.feedback.up': '这个回答有帮助',
  'chat.feedback.down': '这个回答没帮助',
  'chat.feedback.thanks': '感谢反馈',
  'chat.feedback.reasonPlaceholder': '哪里不对？（可不填）',
  'chat.feedback.reasonSubmit': '提交',
  'chat.feedback.reasonSkip': '跳过',
  'chat.feedback.failed': '反馈没能记录下来，请再试一次。',

  // 示例提示词分组

  // 知识库

  // 天气

  // 浏览器

  // 代码执行器示例（code-interpreter 技能 → execute_python 工具）

  // 图片分析
  'chat.trace': "这个回答是怎么来的",
  'chat.typing': '思考中…',
  'chat.consulting': '正在询问',
  'chat.sendFailed': '消息发送失败',
  'chat.voiceMode.enter': '切换到语音模式',
  'chat.voiceMode.exit': '切换到文本模式',
  'chat.voiceMode.connecting': '正在连接语音助手...',
  'chat.voiceMode.connected': '正在聆听，请开口说话。',
  'chat.voiceMode.disconnected': '语音会话已结束。',
  'chat.voiceMode.micDenied': '麦克风权限被拒绝。',
  'chat.voiceMode.error': '语音会话出错',
  'chat.attachImage': '附加图片',
  'chat.image.tooBig': '{name} — 超过 20 MB',
  'chat.image.badFormat': '{name} — 不支持的格式',
  'chat.image.tooMany': '每条消息最多 3 张图片',
  'chat.image.readFailed': '{name} — 无法读取',
  'chat.image.remove': '删除图片',

  // Browser panel
  'browserPanel.title': '浏览器',
  'browserPanel.liveView': '实时画面',
  'browserPanel.files': '文件',
  'browserPanel.noActive': '当前没有正在运行的浏览器会话。',
  'browserPanel.opening': '正在打开浏览器...',
  'browserPanel.goal': '目标',
  'browserPanel.refresh': '刷新',
  'browserPanel.download': '下载',
  'browserPanel.parent': '上级',
  'browserPanel.errorLoad': '无法加载文件列表。',
  'browserPanel.emptyDir': '目录为空。',
  'browserPanel.sessionEnded': '浏览器会话已结束。本次运行生成的文件仍可在"文件"标签页查看。',
  'browserPanel.takeControl': '接管控制',
  'browserPanel.releaseControl': '交还给 Agent',
  'browserPanel.humanMode': '手动控制中 — Agent 已暂停。',
  'browserPanel.idleMode': 'Agent 已完成 — 浏览器仍可操作，直到会话超时。',
  'browserPanel.collapse': '折叠',
  'browserPanel.maximize': '最大化',
  'browserPanel.restore': '还原',

  // 代码执行器面板
  'codePanel.title': '代码执行器',
  'codePanel.task': '任务',
  'codePanel.step': '步骤',
  'codePanel.running': '执行中',
  'codePanel.done': '完成',
  'codePanel.failed': '失败',
  'codePanel.executing': '正在执行…',
  'codePanel.noActive': '当前没有代码在运行。试试“代码执行器”示例，即可在此实时查看执行过程。',
};

export default zh;
