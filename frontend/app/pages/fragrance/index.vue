<template>
  <div>
    <!-- Page header -->
    <div class="d-flex align-center mb-8">
      <div>
        <h1 class="text-h3 font-weight-regular">My Collection</h1>
        <p class="text-caption text-medium-emphasis mt-1" style="letter-spacing: 0.08em; text-transform: uppercase;">
          Personal fragrance library
        </p>
      </div>
      <v-spacer />
      <v-btn
        color="primary"
        variant="flat"
        prepend-icon="mdi-plus"
        rounded="lg"
        @click="openCreate"
      >
        Add fragrance
      </v-btn>
    </div>

    <!-- Filtering tabs -->
    <v-tabs
      v-model="activeTab"
      color="primary"
      density="compact"
      class="mb-6"
    >
      <v-tab value="">All</v-tab>
      <v-tab value="own">Own</v-tab>
      <v-tab value="like">Like</v-tab>
      <v-tab value="dislike">Dislike</v-tab>
    </v-tabs>

    <AppAlert v-model="error" class="mb-6" />

    <!-- Loading state: grid of card skeletons mirroring populated layout -->
    <v-row v-if="loading" class="ga-4">
      <v-col
        v-for="n in 6"
        :key="n"
        cols="12"
        sm="6"
        lg="4"
      >
        <v-skeleton-loader type="card" rounded="lg" elevation="1" />
      </v-col>
    </v-row>

    <!-- Empty state -->
    <v-fade-transition>
      <div
        v-if="!loading && !fragrances.length && !error"
        class="d-flex flex-column align-center justify-center text-center py-16"
      >
        <v-icon
          icon="mdi-spray"
          size="64"
          class="text-medium-emphasis mb-4"
        />
        <p class="text-body-1 text-medium-emphasis mb-6">
          Your collection is empty.
        </p>
        <div class="d-flex ga-3">
          <v-btn
            color="primary"
            variant="flat"
            prepend-icon="mdi-plus"
            rounded="lg"
            @click="openCreate"
          >
            Add fragrance
          </v-btn>
          <v-btn
            variant="outlined"
            prepend-icon="mdi-upload"
            rounded="lg"
            @click="importDialog = true"
          >
            Import from CSV
          </v-btn>
        </div>
      </div>
    </v-fade-transition>

    <!-- Import dialog -->
    <v-dialog v-model="importDialog" max-width="440">
      <v-card rounded="xl">
        <v-card-title class="pt-6 px-6 pb-1 text-h5 font-weight-regular">
          Import from CSV
        </v-card-title>
        <v-card-subtitle class="px-6 pb-4">
          Required columns: <code>fragrance, status, house, notes</code><br>
          Status values: <code>own</code>, <code>like</code>, <code>dislike</code>
        </v-card-subtitle>
        <v-card-text class="px-6 pb-2">
          <v-file-input
            v-model="importFile"
            label="CSV file"
            accept=".csv"
            variant="outlined"
            rounded="lg"
            show-size
            prepend-icon=""
            prepend-inner-icon="mdi-file-delimited-outline"
            hide-details="auto"
            @update:model-value="importReset()"
          />
          <AppAlert v-model="importError" class="mt-4" />
        </v-card-text>
        <v-card-actions class="px-6 pb-6 pt-4">
          <v-spacer />
          <v-btn variant="text" @click="closeImportDialog">Cancel</v-btn>
          <v-btn
            color="primary"
            variant="flat"
            rounded="lg"
            :loading="importLoading"
            :disabled="!importFile"
            @click="runImport"
          >
            Upload
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- Import result snackbar -->
    <v-snackbar v-model="snackbar" :timeout="4000" location="bottom">
      {{ snackbarText }}
    </v-snackbar>

    <!-- Populated state: card grid -->
    <v-fade-transition>
      <v-row v-if="!loading && fragrances.length" class="ga-4">
        <v-col
          v-for="item in fragrances"
          :key="item.id"
          cols="12"
          sm="6"
          lg="4"
        >
          <v-card
            elevation="1"
            rounded="lg"
            height="100%"
            class="d-flex flex-column"
          >
            <v-card-text class="pa-6 flex-grow-1">
              <div class="d-flex align-start justify-space-between mb-1">
                <p class="text-h6 font-weight-medium" style="line-height: 1.3;">
                  {{ item.name }}
                </p>
                <v-chip
                  :color="statusColor(item.status)"
                  variant="tonal"
                  size="small"
                  class="ml-3 mt-1 flex-shrink-0"
                >
                  {{ item.status }}
                </v-chip>
              </div>
              <p class="text-body-2 text-medium-emphasis mb-3">{{ item.house }}</p>
              <p v-if="item.notes" class="text-body-2 mt-2" style="line-height: 1.6;">
                {{ item.notes }}
              </p>
            </v-card-text>

            <v-card-actions class="px-4 pb-4 pt-0">
              <v-spacer />
              <v-btn
                icon="mdi-pencil-outline"
                size="small"
                variant="text"
                color="secondary"
                @click="openEdit(item)"
              />
              <v-btn
                icon="mdi-delete-outline"
                size="small"
                variant="text"
                color="error"
                @click="confirmDelete(item)"
              />
            </v-card-actions>
          </v-card>
        </v-col>
      </v-row>
    </v-fade-transition>

    <!-- Add / Edit dialog -->
    <v-dialog v-model="dialog" max-width="480">
      <v-card rounded="xl">
        <v-card-title class="pt-6 px-6 pb-2 text-h5 font-weight-regular">
          {{ editTarget ? 'Edit fragrance' : 'Add fragrance' }}
        </v-card-title>
        <v-card-text class="px-6 pb-2">
          <v-form ref="form" @submit.prevent="save">
            <v-text-field
              v-model="draft.name"
              label="Name"
              variant="outlined"
              rounded="lg"
              :rules="[required]"
              hide-details="auto"
              class="mb-4"
            />
            <v-text-field
              v-model="draft.house"
              label="House"
              variant="outlined"
              rounded="lg"
              :rules="[required]"
              hide-details="auto"
              class="mb-4"
            />
            <v-select
              v-model="draft.status"
              label="Status"
              variant="outlined"
              rounded="lg"
              :items="statusOptions"
              :rules="[required]"
              hide-details="auto"
              class="mb-4"
            />
            <v-textarea
              v-model="draft.notes"
              label="Notes"
              variant="outlined"
              rounded="lg"
              rows="3"
              hide-details="auto"
            />
          </v-form>
        </v-card-text>
        <v-card-actions class="px-6 pb-6 pt-4">
          <v-spacer />
          <v-btn variant="text" @click="dialog = false">Cancel</v-btn>
          <v-btn
            color="primary"
            variant="flat"
            rounded="lg"
            :loading="saving"
            @click="save"
          >
            Save
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- Delete confirmation dialog -->
    <v-dialog v-model="deleteDialog" max-width="380">
      <v-card rounded="xl">
        <v-card-text class="pa-6">
          <p class="text-h6 font-weight-regular mb-2">Remove from collection?</p>
          <p class="text-body-1 mb-1">
            <strong>{{ deleteTarget?.name }}</strong>
          </p>
          <p class="text-body-2 text-medium-emphasis">
            {{ deleteTarget?.house }}
          </p>
          <p class="text-body-2 text-medium-emphasis mt-3">
            This will permanently remove the fragrance from your collection.
          </p>
        </v-card-text>
        <v-card-actions class="px-6 pb-6 pt-0">
          <v-spacer />
          <v-btn variant="text" @click="deleteDialog = false">Cancel</v-btn>
          <v-btn
            color="error"
            variant="flat"
            rounded="lg"
            :loading="deleting"
            @click="doDelete"
          >
            Remove
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>

<script setup lang="ts">
import { useFragrance, type Fragrance, type FragranceInput } from '~/composables/useFragrance'
import { useImport } from '~/composables/useImport'

const { fragrances, loading, error, fetchFragrances, createFragrance, updateFragrance, deleteFragrance } = useFragrance()
const { loading: importLoading, error: importError, result: importResult, importCollection, reset: importReset } = useImport()

// --- Import ---
const importDialog = ref(false)
const importFile = ref<File | null>(null)
const snackbar = ref(false)
const snackbarText = ref('')

function closeImportDialog(): void {
  importDialog.value = false
  importFile.value = null
  importReset()
}

async function runImport(): Promise<void> {
  const file = importFile.value
  if (!file) return
  await importCollection(file)
  if (importResult.value) {
    const { created, updated, skipped } = importResult.value
    snackbarText.value = [
      created && `${created} added`,
      updated && `${updated} updated`,
      skipped && `${skipped} skipped`,
    ].filter(Boolean).join(', ')
    snackbar.value = true
    closeImportDialog()
    await fetchFragrances()
  }
}

const statusOptions = [
  { title: 'Own', value: 'own' },
  { title: 'Like', value: 'like' },
  { title: 'Dislike', value: 'dislike' },
]

const activeTab = ref('')

watch(activeTab, (val) => fetchFragrances(val || undefined))
onMounted(() => fetchFragrances())

function statusColor(status: string): string {
  const map: Record<string, string> = { own: 'success', like: 'info', dislike: 'error' }
  return map[status] ?? 'default'
}

// --- Add / Edit ---
const dialog = ref(false)
const saving = ref(false)
const editTarget = ref<Fragrance | null>(null)
const form = ref()
const draft = reactive<FragranceInput>({ name: '', house: '', status: 'own', notes: '' })

const required = (v: string) => !!v || 'Required'

function openCreate(): void {
  editTarget.value = null
  Object.assign(draft, { name: '', house: '', status: 'own', notes: '' })
  dialog.value = true
}

function openEdit(item: Fragrance): void {
  editTarget.value = item
  Object.assign(draft, { name: item.name, house: item.house, status: item.status, notes: item.notes })
  dialog.value = true
}

async function save(): Promise<void> {
  const { valid } = await form.value.validate()
  if (!valid) return
  saving.value = true
  try {
    if (editTarget.value) {
      const updated = await updateFragrance(editTarget.value.id, { ...draft })
      const idx = fragrances.value.findIndex((f) => f.id === updated.id)
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

function confirmDelete(item: Fragrance): void {
  deleteTarget.value = item
  deleteDialog.value = true
}

async function doDelete(): Promise<void> {
  if (!deleteTarget.value) return
  deleting.value = true
  try {
    await deleteFragrance(deleteTarget.value.id)
    fragrances.value = fragrances.value.filter((f) => f.id !== deleteTarget.value!.id)
    deleteDialog.value = false
  } finally {
    deleting.value = false
  }
}
</script>
