CREATE MIGRATION m1ugbehu7mnhuuv27nfapzxq4ukxlooxemzp65p3idmnfhcdegq5ha
    ONTO m1p253uxc57fhcdmohbmoqoltpgy42tzm3mqb6kdej7piccdg5sbca
{
  ALTER TYPE telegram::MessageLog {
      DROP PROPERTY caption;
  };
};
