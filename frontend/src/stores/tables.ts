import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { DataTable, TableSchema, TableDataResponse, PaginationParams, PaginatedResponse } from '@/types'
import { tablesApi } from '@/api/tables'
import { useStoreAction, useStoreList } from '@/composables/useStoreAction'

export const useTableStore = defineStore(
  'tables',
  () => {
    const listHelper = useStoreList<DataTable>({ defaultPageSize: 20 })
    const actionHelper = useStoreAction()
    const schemaAction = useStoreAction()
    const dataAction = useStoreAction()

    const currentTable = ref<DataTable | null>(null)
    const currentSchema = ref<TableSchema | null>(null)
    const currentData = ref<TableDataResponse | null>(null)
    const searchQuery = ref('')

    const hasMore = computed(() => listHelper.items.value.length < listHelper.total.value)
    const tableNames = computed(() => listHelper.items.value.map((t) => t.table_name))
    const loading = computed(() => listHelper.loading.value || actionHelper.loading.value)
    const schemaLoading = computed(() => schemaAction.loading.value)
    const dataLoading = computed(() => dataAction.loading.value)
    const error = computed(
      () => listHelper.error.value || actionHelper.error.value || schemaAction.error.value || dataAction.error.value
    )

    async function fetchTables(params?: PaginationParams & { search?: string }) {
      if (params?.page !== undefined) {
        listHelper.setPage(params.page)
      }
      await listHelper.load(
        async ({ page, pageSize }) => {
          const response: PaginatedResponse<DataTable> = await tablesApi.list({
            page,
            page_size: pageSize,
            search: searchQuery.value || undefined,
            ...params,
          })
          return { items: response.items, total: response.total }
        },
        { errorMessage: '获取数据表列表失败' }
      )
    }

    async function fetchTableSchema(tableId: string | number) {
      await schemaAction.execute(
        async () => {
          currentSchema.value = await tablesApi.getSchema(tableId)
          return currentSchema.value
        },
        { errorMessage: '获取表结构失败' }
      )
    }

    async function fetchTableData(tableId: string | number, pageNum?: number) {
      await dataAction.execute(
        async () => {
          currentData.value = await tablesApi.getData(tableId, pageNum ?? listHelper.page.value, listHelper.pageSize.value)
          return currentData.value
        },
        { errorMessage: '获取表数据失败' }
      )
    }

    async function deleteTable(tableId: string | number) {
      await actionHelper.execute(
        async () => {
          await tablesApi.delete(tableId)
          listHelper.items.value = listHelper.items.value.filter((t) => t.id !== tableId && t.table_name !== tableId)
          listHelper.total.value--
          if (currentTable.value?.id === tableId || currentTable.value?.table_name === tableId) {
            currentTable.value = null
            currentSchema.value = null
            currentData.value = null
          }
        },
        { errorMessage: '删除数据表失败' }
      )
    }

    function setCurrentTable(table: DataTable | null) {
      currentTable.value = table
    }

    function setSearchQuery(query: string) {
      searchQuery.value = query
      listHelper.setPage(1)
    }

    function reset() {
      listHelper.reset()
      actionHelper.reset()
      schemaAction.reset()
      dataAction.reset()
      currentTable.value = null
      currentSchema.value = null
      currentData.value = null
      searchQuery.value = ''
    }

    return {
      tables: listHelper.items,
      currentTable,
      currentSchema,
      currentData,
      total: listHelper.total,
      loading,
      schemaLoading,
      dataLoading,
      error,
      page: listHelper.page,
      pageSize: listHelper.pageSize,
      searchQuery,
      hasMore,
      tableNames,
      fetchTables,
      fetchTableSchema,
      fetchTableData,
      deleteTable,
      setCurrentTable,
      setSearchQuery,
      setPage: listHelper.setPage,
      setPageSize: listHelper.setPageSize,
      reset,
    }
  },
  {
    persist: {
      paths: ['page', 'pageSize', 'searchQuery'],
    },
  }
)
