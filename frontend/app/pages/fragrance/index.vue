<template>
  <div>
    <div class="d-flex align-center mb-6">
      <h1 class="text-h4">My Collection</h1>
      <v-spacer />
      <v-btn color="primary" prepend-icon="mdi-plus" @click="openCreate">Add Fragrance</v-btn>
    </div>

    <v-tabs v-model="activeTab" class="mb-4">
      <v-tab value="">All</v-tab>
      <v-tab value="own">Own</v-tab>
      <v-tab value="like">Like</v-tab>
      <v-tab value="dislike">Dislike</v-tab>
    </v-tabs>

    <v-alert v-if="error" type="error" class="mb-4">{{ error }}</v-alert>

    <v-data-table
      :headers="headers"
      :items="fragrances"
      :loading="loading"
      no-data-text="No fragrances yet — add one above."
    >
      <template #item.status="{ value }">
        <v-chip :color="statusColor(value)" size="small">{{ value }}</v-chip>
      </template>

      <template #item.actions="{ item }">
        <v-btn icon size="small" variant="text" @click="openEdit(item)">
          <v-icon>mdi-pencil</v-icon>
        </v-btn>
        <v-btn icon size="small" variant="text" color="error" @click="confirmDelete(item)">
          <v-icon>mdi-delete</v-icon>
        </v-btn>
      </template>
    </v-data-table>

    <!-- Add / Edit dialog -->
    <v-dialog v-model="dialog" max-width="480">
      <v-card>
        <v-card-title>{{ editTarget ? 'Edit Fragrance' : 'Add Fragrance' }}</v-card-title>
        <v-card-text>
          <v-form ref="form" @submit.prevent="save">
            <v-text-field v-model="draft.name" label="Name" :rules="[required]" class="mb-2" />
            <v-text-field v-model="draft.house" label="House" :rules="[required]" class="mb-2" />
            <v-select
              v-model="draft.status"
              label="Status"
              :items="statusOptions"
              :rules="[required]"
              class="mb-2"
            />
            <v-textarea v-model="draft.notes" label="Notes" rows="3" />
          </v-form>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="dialog = false">Cancel</v-btn>
          <v-btn color="primary" :loading="saving" @click="save">Save</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- Delete confirmation dialog -->
    <v-dialog v-model="deleteDialog" max-width="360">
      <v-card>
        <v-card-title>Delete fragrance?</v-card-title>
        <v-card-text>
          "{{ deleteTarget?.name }}" will be permanently removed from your collection.
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="deleteDialog = false">Cancel</v-btn>
          <v-btn color="error" :loading="deleting" @click="doDelete">Delete</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>

<script setup lang="ts">
import { useFragrance, type Fragrance, type FragranceInput } from '~/composables/useFragrance'

const { fragrances, loading, error, fetchFragrances, createFragrance, updateFragrance, deleteFragrance } = useFragrance()

const headers = [
  { title: 'Name', key: 'name' },
  { title: 'House', key: 'house' },
  { title: 'Status', key: 'status' },
  { title: 'Notes', key: 'notes' },
  { title: '', key: 'actions', sortable: false, align: 'end' as const },
]

const statusOptions = [
  { title: 'Own', value: 'own' },
  { title: 'Like', value: 'like' },
  { title: 'Dislike', value: 'dislike' },
]

const activeTab = ref('')

watch(activeTab, (val) => fetchFragrances(val || undefined))
onMounted(() => fetchFragrances())

function statusColor(status: string) {
  return { own: 'success', like: 'info', dislike: 'error' }[status] ?? 'default'
}

// --- Add / Edit ---
const dialog = ref(false)
const saving = ref(false)
const editTarget = ref<Fragrance | null>(null)
const form = ref()
const draft = reactive<FragranceInput>({ name: '', house: '', status: 'own', notes: '' })

const required = (v: string) => !!v || 'Required'

function openCreate() {
  editTarget.value = null
  Object.assign(draft, { name: '', house: '', status: 'own', notes: '' })
  dialog.value = true
}

function openEdit(item: Fragrance) {
  editTarget.value = item
  Object.assign(draft, { name: item.name, house: item.house, status: item.status, notes: item.notes })
  dialog.value = true
}

async function save() {
  const { valid } = await form.value.validate()
  if (!valid) return
  saving.value = true
  try {
    if (editTarget.value) {
      const updated = await updateFragrance(editTarget.value.id, { ...draft })
      const idx = fragrances.value.findIndex(f => f.id === updated.id)
      if (idx !== -1) fragrances.value[idx] = updated
    } else {
      const created = await createFragrance({ ...draft })
      fragrances.value.unshift(created)
    }
    dialog.value = false
  } finally {
    saving.value = false
  }
}

// --- Delete ---
const deleteDialog = ref(false)
const deleting = ref(false)
const deleteTarget = ref<Fragrance | null>(null)

function confirmDelete(item: Fragrance) {
  deleteTarget.value = item
  deleteDialog.value = true
}

async function doDelete() {
  if (!deleteTarget.value) return
  deleting.value = true
  try {
    await deleteFragrance(deleteTarget.value.id)
    fragrances.value = fragrances.value.filter(f => f.id !== deleteTarget.value!.id)
    deleteDialog.value = false
  } finally {
    deleting.value = false
  }
}
</script>
