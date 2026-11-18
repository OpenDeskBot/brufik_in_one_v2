#pragma once

// Ordering is independent of FFat/NVS so failures and resets can be injected on
// the host. A backend must make select_slot atomic; neither slot is live until
// its file AND metadata have been published and verified.
namespace face_cache_policy {
template<class Store> int recover(Store& store, unsigned preferred) {
  for (unsigned i=0;i<2;++i) {
    const unsigned slot=(preferred+i)%2;
    if (store.load_if_valid(slot)) return (int)slot;
  }
  return -1;
}
template<class Store> bool commit(Store& store, int current) {
  const unsigned next=current==0?1:0;
  return store.write_temporary() && store.verify_temporary() &&
      store.replace_slot(next) && store.write_metadata(next) && store.select_slot(next);
}
} // namespace face_cache_policy
