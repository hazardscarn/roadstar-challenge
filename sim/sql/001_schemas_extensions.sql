create extension if not exists postgis;
create extension if not exists pg_cron;   -- TEST TODAY: not confirmed available on this project's tier

create schema if not exists reference;
create schema if not exists ground_truth;
create schema if not exists calibration;
create schema if not exists sim;
create schema if not exists live;
