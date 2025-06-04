CREATE MIGRATION m15hckuagc34aosb5bpltegbfv3un4a36v5kqwhjofl2atinvrbz3a
    ONTO m1qos4fo26wo7eugx4zhgmkcnuaxasochdrnm3zvhxtyrrn3bd5g4a
{
  ALTER TYPE meta::HasMetadata {
      DROP PROPERTY metadata;
  };
  CREATE ABSTRACT TYPE telegram::ChatSettings {
      CREATE PROPERTY llm_memory: std::str {
          CREATE CONSTRAINT std::max_len_value(1024);
      };
      CREATE ANNOTATION std::description := 'Chat settings, adjustable by participants';
      CREATE ANNOTATION std::title := 'Telegram Chat Settings';
  };
  ALTER TYPE telegram::Chat {
      DROP EXTENDING meta::HasMetadata;
      EXTENDING telegram::ChatSettings LAST;
  };
  ALTER TYPE telegram::User DROP EXTENDING meta::HasMetadata;
  DROP TYPE meta::HasMetadata;
};
