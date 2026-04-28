import React from 'react';
import { useI18n } from '../i18n';

export type ErpTab = 'skills' | 'a2a';

interface Props {
  active: ErpTab;
  onChange: (t: ErpTab) => void;
}

const TabBar: React.FC<Props> = ({ active, onChange }) => {
  const { t } = useI18n();
  return (
    <div className="tab-bar">
      <button
        className={`tab ${active === 'skills' ? 'tab-active' : ''}`}
        onClick={() => onChange('skills')}
      >
        {t('erp.tab.skills')}
      </button>
      <button
        className={`tab ${active === 'a2a' ? 'tab-active' : ''}`}
        onClick={() => onChange('a2a')}
      >
        {t('erp.tab.a2a')}
      </button>
    </div>
  );
};

export default TabBar;
