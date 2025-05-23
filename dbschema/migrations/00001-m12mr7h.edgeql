CREATE MIGRATION m12mr7h7k6qidvnuxnng2k2dm6mp5wgxhthdydhsm23tiryobze4uq
    ONTO initial
{
  CREATE MODULE meta IF NOT EXISTS;
  CREATE MODULE telegram IF NOT EXISTS;
  CREATE ABSTRACT TYPE meta::HasCreated {
      CREATE REQUIRED PROPERTY created_at: std::datetime {
          CREATE REWRITE
              INSERT 
              USING (std::datetime_of_statement());
      };
      CREATE INDEX ON (.created_at);
  };
  CREATE ABSTRACT TYPE meta::HasMetadata {
      CREATE PROPERTY metadata: std::json {
          SET default := (<std::json>'{}');
      };
  };
  CREATE ABSTRACT TYPE meta::HasUpdated {
      CREATE REQUIRED PROPERTY updated_at: std::datetime {
          CREATE REWRITE
              INSERT 
              USING (std::datetime_of_statement());
          CREATE REWRITE
              UPDATE 
              USING ((std::datetime_of_statement() IF (<std::json>__subject__ {
                  **
              } != <std::json>__old__ {
                  **
              }) ELSE __old__.updated_at));
      };
      CREATE INDEX ON (.updated_at);
  };
  CREATE ABSTRACT TYPE meta::HasExpiration {
      CREATE PROPERTY expires_at: std::datetime {
          SET default := ((std::datetime_of_statement() + <std::cal::relative_duration>'30 days'));
      };
      CREATE PROPERTY is_expired := ((.expires_at < std::datetime_of_statement()));
      CREATE INDEX ON (.expires_at);
  };
  CREATE TYPE telegram::BotUpdate EXTENDING meta::HasCreated, meta::HasExpiration {
      CREATE PROPERTY handled: std::bool {
          SET default := false;
      };
      CREATE REQUIRED PROPERTY raw_data: std::json;
      CREATE REQUIRED PROPERTY update_id: std::int64;
      CREATE REQUIRED PROPERTY update_type: std::str;
      CREATE ANNOTATION std::description := 'Represents an incoming Telegram update with TTL';
      CREATE ANNOTATION std::title := 'Telegram Update';
      CREATE INDEX ON ((.update_type, .handled));
      CREATE INDEX ON (.handled);
      CREATE INDEX ON (.update_type);
  };
  CREATE ALIAS telegram::ActiveBotUpdates := (
      SELECT
          telegram::BotUpdate
      FILTER
          NOT (.is_expired)
  );
  CREATE ALIAS telegram::ExpiredBotUpdates := (
      SELECT
          telegram::BotUpdate
      FILTER
          .is_expired
  );
  CREATE TYPE telegram::Chat EXTENDING meta::HasCreated, meta::HasUpdated, meta::HasMetadata {
      CREATE ANNOTATION std::description := 'Represents a Telegram chat';
      CREATE ANNOTATION std::title := 'Telegram Chat';
      CREATE REQUIRED PROPERTY chat_id: std::int64 {
          CREATE CONSTRAINT std::exclusive;
      };
      CREATE INDEX ON (.chat_id);
      CREATE PROPERTY first_name: std::str;
      CREATE PROPERTY last_name: std::str;
      CREATE PROPERTY title: std::str;
      CREATE PROPERTY username: std::str;
      CREATE PROPERTY display_name := ((.title IF EXISTS (.title) ELSE (('@' ++ .username) IF EXISTS (.username) ELSE (.first_name ++ ((' ' ++ .last_name) IF EXISTS (.last_name) ELSE '')))));
      CREATE PROPERTY is_forum: std::bool {
          SET default := false;
      };
      CREATE REQUIRED PROPERTY type: std::str;
  };
  CREATE TYPE telegram::User EXTENDING meta::HasCreated, meta::HasUpdated, meta::HasMetadata {
      CREATE ANNOTATION std::description := 'Represents a Telegram user or bot';
      CREATE ANNOTATION std::title := 'Telegram User';
      CREATE REQUIRED PROPERTY user_id: std::int64 {
          CREATE CONSTRAINT std::exclusive;
      };
      CREATE INDEX ON (.user_id);
      CREATE PROPERTY added_to_attachment_menu: std::bool {
          SET default := false;
      };
      CREATE REQUIRED PROPERTY first_name: std::str;
      CREATE PROPERTY last_name: std::str;
      CREATE PROPERTY full_name := ((.first_name ++ ((' ' ++ .last_name) IF EXISTS (.last_name) ELSE '')));
      CREATE PROPERTY username: std::str;
      CREATE PROPERTY display_name := ((('@' ++ .username) IF EXISTS (.username) ELSE .full_name));
      CREATE REQUIRED PROPERTY is_bot: std::bool;
      CREATE PROPERTY is_premium: std::bool {
          SET default := false;
      };
      CREATE PROPERTY language_code: std::str;
  };
  ALTER TYPE telegram::BotUpdate {
      CREATE LINK chat: telegram::Chat {
          ON TARGET DELETE ALLOW;
      };
      CREATE LINK from_user: telegram::User {
          ON TARGET DELETE ALLOW;
      };
      CREATE INDEX ON (.from_user);
      CREATE INDEX ON (.chat);
  };
  ALTER TYPE telegram::Chat {
      CREATE MULTI LINK updates := (.<chat[IS telegram::BotUpdate]);
  };
  ALTER TYPE telegram::User {
      CREATE MULTI LINK updates := (.<from_user[IS telegram::BotUpdate]);
  };
};
