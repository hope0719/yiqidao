CREATE TABLE `limits` (
	`id` text PRIMARY KEY NOT NULL,
	`count` integer DEFAULT 0 NOT NULL
);
--> statement-breakpoint
CREATE TABLE `trips` (
	`id` text PRIMARY KEY NOT NULL,
	`owner` text NOT NULL,
	`session` text,
	`status` text NOT NULL,
	`request` text NOT NULL,
	`result` text,
	`error` text,
	`created` integer NOT NULL,
	`updated` integer NOT NULL,
	`expires` integer NOT NULL,
	`kind` text NOT NULL,
	`share` text,
	`saved` integer DEFAULT 0 NOT NULL,
	`cursor` text,
	`started` integer DEFAULT 0 NOT NULL
);
