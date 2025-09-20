CREATE MIGRATION m1p253uxc57fhcdmohbmoqoltpgy42tzm3mqb6kdej7piccdg5sbca
    ONTO m15hckuagc34aosb5bpltegbfv3un4a36v5kqwhjofl2atinvrbz3a
{
  CREATE TYPE telegram::MessageLog EXTENDING meta::HasCreated, meta::HasUpdated {
      CREATE ANNOTATION std::description := 'Cleaned messages and edits for easy querying';
      CREATE ANNOTATION std::title := 'Conversation Message Log';
      CREATE REQUIRED LINK chat: telegram::Chat {
          ON TARGET DELETE ALLOW;
      };
      CREATE REQUIRED PROPERTY message_id: std::int64;
      CREATE INDEX ON ((.chat, .message_id));
      CREATE REQUIRED PROPERTY message_key: std::str {
          CREATE CONSTRAINT std::exclusive;
      };
      CREATE INDEX ON (.message_key);
      CREATE PROPERTY media_group_id: std::str;
      CREATE INDEX ON (.media_group_id);
      CREATE INDEX ON (.chat);
      CREATE LINK from_user: telegram::User {
          ON TARGET DELETE ALLOW;
      };
      CREATE LINK source_update: telegram::BotUpdate {
          ON TARGET DELETE ALLOW;
      };
      CREATE PROPERTY attachment_file_id: std::str;
      CREATE PROPERTY attachment_type: std::str;
      CREATE PROPERTY caption: std::str;
      CREATE PROPERTY content_type: std::str {
          SET default := 'text';
      };
      CREATE PROPERTY deleted_at: std::datetime;
      CREATE PROPERTY is_deleted := (EXISTS (.deleted_at));
      CREATE REQUIRED PROPERTY direction: std::str;
      CREATE PROPERTY edited_at: std::datetime;
      CREATE PROPERTY reply_to_message_id: std::int64;
      CREATE PROPERTY text: std::str;
      CREATE PROPERTY tg_date: std::datetime;
      CREATE PROPERTY thread_id: std::int64;
  };
};
