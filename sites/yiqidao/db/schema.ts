import { sqliteTable, text, integer } from 'drizzle-orm/sqlite-core';
export const trips=sqliteTable('trips',{
 id:text('id').primaryKey(), owner:text('owner').notNull(), session:text('session'), status:text('status').notNull(), request:text('request').notNull(), result:text('result'), error:text('error'), created:integer('created').notNull(), updated:integer('updated').notNull(), expires:integer('expires').notNull(), kind:text('kind').notNull(), share:text('share'), saved:integer('saved').notNull().default(0), cursor:text('cursor'), started:integer('started').notNull().default(0)
});
export const limits=sqliteTable('limits',{id:text('id').primaryKey(),count:integer('count').notNull().default(0)});
