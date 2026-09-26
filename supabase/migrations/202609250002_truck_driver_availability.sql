-- Driver and vehicle identity (never auto-shared) plus live truck availability.
alter table freight_truck_profiles add column if not exists truck_vin text not null default '';
alter table freight_truck_profiles add column if not exists driver_name text not null default '';
alter table freight_truck_profiles add column if not exists driver_cdl_number text not null default '';
alter table freight_truck_profiles add column if not exists driver_cdl_state text not null default '';
alter table freight_truck_profiles add column if not exists driver_phone text not null default '';
alter table freight_truck_profiles add column if not exists availability_status text not null default 'available';
alter table freight_truck_profiles add column if not exists available_from date;
